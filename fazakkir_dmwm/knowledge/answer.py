"""The Ask DMWM answer service: retrieval + LLM + interaction logging."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from .models import AnswerFeedback, AskInteraction, AskSettings
from .retrieval import build_context

logger = logging.getLogger(__name__)

MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CHARS = 12000
MAX_QUESTION_CHARS = 3000


@dataclass(frozen=True)
class ChatProvider:
    name: str
    url: str
    key: str
    model: str


def chat_provider() -> ChatProvider | None:
    """Pick the configured chat provider.

    Preference order favours the free, higher-quality providers (Gemini and
    OpenRouter) over Groq, then falls back to OpenAI.
    """
    groq_key = (
        getattr(settings, "GROQ_API_KEY", "")
        or os.environ.get("GROQ_API_KEY", "")
    ).strip()
    gemini_key = (
        getattr(settings, "GEMINI_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
    ).strip()
    openrouter_key = (
        getattr(settings, "OPENROUTER_API_KEY", "")
        or os.environ.get("OPENROUTER_API_KEY", "")
    ).strip()
    openai_key = (
        getattr(settings, "OPENAI_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    ).strip()

    if gemini_key:
        return ChatProvider(
            name="gemini",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            key=gemini_key,
            model=getattr(settings, "GEMINI_ASK_MODEL", "gemini-3.5-flash"),
        )
    if openrouter_key:
        return ChatProvider(
            name="openrouter",
            url="https://openrouter.ai/api/v1/chat/completions",
            key=openrouter_key,
            model=getattr(settings, "OPENROUTER_ASK_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        )
    if groq_key:
        return ChatProvider(
            name="groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            key=groq_key,
            model=getattr(settings, "GROQ_ASK_MODEL", "openai/gpt-oss-20b"),
        )
    if openai_key:
        return ChatProvider(
            name="openai",
            url="https://api.openai.com/v1/chat/completions",
            key=openai_key,
            model=getattr(settings, "OPENAI_ASK_MODEL", "gpt-4o-mini"),
        )
    return None


def clean_history(raw: str | None) -> list[dict[str, str]]:
    """Validate the conversation sent by the browser."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []

    history: list[dict[str, str]] = []
    total = 0
    for item in value[-MAX_HISTORY_ITEMS:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        content = content[:4000]
        total += len(content)
        if total > MAX_HISTORY_CHARS:
            break
        history.append({"role": role, "content": content})
    return history


def _messages(system: str, history: list[dict[str, str]], user_content: str):
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})
    return messages


def _call_provider(
    provider: ChatProvider,
    system: str,
    history: list[dict[str, str]],
    user_content: str,
    temperature: float,
) -> tuple[str | None, str]:
    try:
        response = httpx.post(
            provider.url,
            headers={
                "Authorization": f"Bearer {provider.key}",
                "Content-Type": "application/json",
            },
            json={
                "model": provider.model,
                "messages": _messages(system, history, user_content),
                "temperature": max(0.0, min(1.0, temperature)),
            },
            timeout=90.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ask provider %s failed: %s", provider.name, exc)
        return None, "The AI assistant is unavailable right now."

    if response.status_code != 200:
        logger.warning(
            "Ask provider %s returned %s: %s",
            provider.name,
            response.status_code,
            response.text[:600],
        )
        return None, "The AI service returned an error."

    try:
        answer = response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Unexpected response from %s: %s", provider.name, exc)
        return None, "The AI service returned an unreadable answer."
    if not answer:
        return None, "The AI service returned an empty answer."
    return answer, ""


def training_examples(question: str = "", limit: int = 3) -> list[tuple[str, str]]:
    """Return editor-approved (question, answer) pairs for few-shot prompting.

    When a question is supplied, the most relevant examples are chosen by
    keyword overlap so the model sees corrections that actually apply instead
    of an arbitrary list.
    """
    rows = list(
        AskInteraction.objects.filter(use_as_example=True)
        .exclude(corrected_answer="")
        .order_by("-created_at")[:50]
    )
    if not rows:
        return []
    if not question:
        return [(row.question, row.corrected_answer) for row in rows[:limit]]

    question_tokens = set(question.lower().split())
    scored = []
    for row in rows:
        text = (row.question + " " + row.corrected_answer).lower()
        tokens = set(text.split())
        overlap = len(question_tokens & tokens)
        scored.append((overlap, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = [row for overlap, row in scored if overlap > 0][:limit]
    if not chosen:
        chosen = [row for _, row in scored[:limit]]
    return [(row.question, row.corrected_answer) for row in chosen]


def feedback_notes(question: str, limit: int = 4) -> list[str]:
    """Collect recent visitor feedback that is relevant to *question*.

    These are treated as soft guidance ("users reported ...") rather than
    authoritative corrections, so the model is told to respect them but not to
    treat them as verified sources.
    """
    rows = list(
        AnswerFeedback.objects.select_related("interaction")
        .order_by("-created_at")[:60]
    )
    if not rows:
        return []
    question_tokens = set((question or "").lower().split())
    scored = []
    for feedback in rows:
        interaction_question = getattr(feedback.interaction, "question", "") or ""
        text_blob = (interaction_question + " " + (feedback.comment or "") + " " + (feedback.suggested_answer or "")).lower()
        tokens = set(text_blob.split())
        overlap = len(question_tokens & tokens) if question_tokens else 0
        scored.append((overlap, feedback))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = [fb for overlap, fb in scored if overlap > 0][:limit]
    if not chosen:
        chosen = [fb for _, fb in scored[:limit]]
    notes: list[str] = []
    for feedback in chosen:
        parts = []
        if feedback.comment:
            parts.append("issue: " + feedback.comment.strip().replace("\n", " ")[:240])
        if feedback.suggested_answer:
            parts.append("suggested answer: " + feedback.suggested_answer.strip().replace("\n", " ")[:400])
        if feedback.helpful is False:
            parts.append("(marked not helpful)")
        if parts:
            notes.append("; ".join(parts))
    return notes


def answer_question(
    *,
    question: str,
    language: str,
    history: list[dict[str, str]],
    user=None,
) -> dict:
    """Answer one question and persist the interaction for review."""
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return {
            "success": False,
            "error": "Ask a question first.",
            "status": 400,
        }

    ask_settings = AskSettings.load()
    if not ask_settings.is_enabled:
        return {
            "success": False,
            "error": "The AI assistant is temporarily unavailable.",
            "status": 503,
        }

    # Guard the paid API against runaway loops from one account.  Counts live
    # in the database so the limit is shared by every gunicorn worker.
    hourly_limit = int(getattr(settings, "AI_ASK_HOURLY_LIMIT", 40) or 0)
    is_staff = bool(getattr(user, "is_staff", False))
    if (
        hourly_limit
        and getattr(user, "is_authenticated", False)
        and not is_staff
        and AskInteraction.objects.filter(
            user=user, created_at__gte=timezone.now() - timedelta(hours=1)
        ).count()
        >= hourly_limit
    ):
        return {
            "success": False,
            "error": "You have reached the question limit for now. Please try again later.",
            "status": 429,
        }

    provider = chat_provider()
    if provider is None:
        return {
            "success": False,
            "error": "The AI assistant is not configured.",
            "status": 503,
        }

    try:
        context = build_context(question, language=language, history=history)
    except Exception:  # noqa: BLE001
        logger.warning("Knowledge retrieval failed", exc_info=True)
        context = {
            "text": "",
            "references": [],
            "internal_count": 0,
            "external_count": 0,
            "retrieval_query": question,
        }

    has_internal = bool(context["internal_count"])
    has_external = bool(context["external_count"])
    has_evidence = bool(context["text"].strip())

    system = ask_settings.effective_prompt
    examples = training_examples(question)
    if examples:
        system += "\n\nADMIN-APPROVED CORRECTIONS (use these as the authoritative answer when the question is similar; do not contradict them):\n"
        for question, answer in examples:
            system += f"Q: {question}\nA: {answer}\n\n"
    notes = feedback_notes(question)
    if notes:
        system += "\n\nVISITOR FEEDBACK (respect these reports; they are not verified sources):\n"
        for note in notes:
            system += f"- {note}\n"
    if has_external:
        system += (
            "\n\nSome evidence is marked EXTERNAL. Use it only to supplement "
            "the internal library, and make clear in the answer when a point "
            "comes from an external web source rather than from Fazakkir."
        )
    if not has_evidence:
        system += (
            "\n\nNo matching sources were retrieved for this question. Do not "
            "invent citations. Answer only with cautious general knowledge, "
            "state clearly that no specific source was found, and recommend a "
            "book, video or qualified scholar for further study."
        )

    prompt_parts = [
        f"Language requested by the user: {language or 'en'}",
        f"Question: {question}",
    ]
    if has_evidence:
        prompt_parts.append(
            "RETRIEVED EVIDENCE (cite as [S1], [S2], …):\n" + context["text"]
        )
        prompt_parts.append(
            "CITATION RULES:\n"
            "- Every substantive religious claim must carry a citation such as [S1].\n"
            "- Cite only the sources listed above. Never create a new number.\n"
            "- When the sources disagree, name both and explain the difference.\n"
            "- If the answer is not in the evidence, say so.\n"
            "- Source passages may be in French, Arabic or Wolof. Translate any quotation into the requested language and mark it as your translation, keeping the [S#] citation.\n"
            "- Never guess or fill in missing details (dates, names, places, events). If the evidence does not state it, say it is not stated in the sources.\n"
            "- Every factual statement must carry a citation such as [S1]. If you cannot cite it, do not state it.\n"
            "- You may add markdown links [title](url) using only URLs present in the evidence, so readers can open the source."
        )
    else:
        prompt_parts.append(
            "No evidence was retrieved. Say that the library produced no matching "
            "passage and give only careful general context without citations."
        )

    user_content = "\n\n".join(prompt_parts)
    answer, error = _call_provider(
        provider,
        system,
        history,
        user_content,
        min(ask_settings.temperature, 0.2),
    )

    status = AskInteraction.STATUS_ERROR
    if answer:
        status = (
            AskInteraction.STATUS_ANSWERED
            if has_evidence
            else AskInteraction.STATUS_NO_CONTEXT
        )

    interaction = AskInteraction.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        question=question,
        answer=answer or "",
        language=(language or "")[:12],
        status=status,
        error=error,
        references=context["references"],
        used_internal=has_internal,
        used_external=has_external,
    )

    if not answer:
        return {
            "success": False,
            "error": error,
            "status": 502,
            "interaction_id": interaction.pk,
        }

    return {
        "success": True,
        "answer": answer,
        "references": context["references"],
        "interaction_id": interaction.pk,
        "used_internal": has_internal,
        "used_external": has_external,
    }
