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
    kind: str = "openai"


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
    anthropic_key = (
        getattr(settings, "ANTHROPIC_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", "")
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
    if anthropic_key:
        return ChatProvider(
            name="claude",
            url="https://api.anthropic.com/v1/messages",
            key=anthropic_key,
            model=getattr(settings, "ANTHROPIC_ASK_MODEL", "claude-sonnet-4-5"),
            kind="anthropic",
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


def all_providers() -> list[ChatProvider]:
    """Return every configured chat provider, in preference order."""
    result: list[ChatProvider] = []
    gemini_key = (getattr(settings, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")).strip()
    anthropic_key = (getattr(settings, "ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    openrouter_key = (getattr(settings, "OPENROUTER_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    groq_key = (getattr(settings, "GROQ_API_KEY", "") or os.environ.get("GROQ_API_KEY", "")).strip()
    openai_key = (getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")).strip()

    if gemini_key:
        result.append(ChatProvider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", gemini_key, getattr(settings, "GEMINI_ASK_MODEL", "gemini-3.5-flash")))
    if anthropic_key:
        result.append(ChatProvider("claude", "https://api.anthropic.com/v1/messages", anthropic_key, getattr(settings, "ANTHROPIC_ASK_MODEL", "claude-sonnet-4-5"), "anthropic"))
    if openrouter_key:
        result.append(ChatProvider("openrouter", "https://openrouter.ai/api/v1/chat/completions", openrouter_key, getattr(settings, "OPENROUTER_ASK_MODEL", "google/gemma-4-31b-it:free")))
    if groq_key:
        result.append(ChatProvider("groq", "https://api.groq.com/openai/v1/chat/completions", groq_key, getattr(settings, "GROQ_ASK_MODEL", "openai/gpt-oss-20b")))
    if openai_key:
        result.append(ChatProvider("openai", "https://api.openai.com/v1/chat/completions", openai_key, getattr(settings, "OPENAI_ASK_MODEL", "gpt-4o-mini")))
    return result


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
    temperature = max(0.0, min(1.0, temperature))
    try:
        if provider.kind == "anthropic":
            response = httpx.post(
                provider.url,
                headers={
                    "x-api-key": provider.key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider.model,
                    "max_tokens": 2048,
                    "system": system,
                    "messages": history + [{"role": "user", "content": user_content}],
                    "temperature": temperature,
                },
                timeout=90.0,
            )
        else:
            response = httpx.post(
                provider.url,
                headers={
                    "Authorization": f"Bearer {provider.key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider.model,
                    "messages": _messages(system, history, user_content),
                    "temperature": temperature,
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
        payload = response.json()
        if provider.kind == "anthropic":
            parts = payload.get("content") or []
            answer = "".join(
                item.get("text", "") for item in parts if item.get("type") == "text"
            ).strip()
        else:
            answer = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError, TypeError) as exc:
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
    # Promoted feedback (reviewed by an admin with a suggested answer) also
    # counts as an approved correction.
    promoted = list(
        AnswerFeedback.objects.filter(is_reviewed=True)
        .exclude(suggested_answer="")
        .select_related("interaction")
        .order_by("-created_at")[:50]
    )
    for feedback in promoted:
        interaction = feedback.interaction
        rows.append(
            type("Fb", (), {
                "question": interaction.question if interaction else "",
                "corrected_answer": feedback.suggested_answer,
            })()
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
            parts.append("CORRECTION (use this as the right answer): " + feedback.suggested_answer.strip().replace("\n", " ")[:400])
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
    multi_model = bool(getattr(settings, "AI_ASK_MULTI_MODEL", False))
    if multi_model:
        providers = all_providers()
    else:
        providers = [provider] if provider is not None else []
    if not providers:
        return {
            "success": False,
            "error": "The AI assistant is not configured.",
            "status": 503,
        }
    provider = providers[0]
    selected_providers = providers[: max(1, int(getattr(settings, "AI_ASK_MULTI_MAX", 3)))] if multi_model else [provider]

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
    system += (
        "\n\nORDER CLARIFICATION: The Fazakkir orators (Serigne Cheikh Ahmad Tidiane Sy Al Maktoum, "
        "Serigne Moustapha Sy, Serigne Cheikh Ahmad Tidiane Sy Capitaine) belong to the Tijaniyya "
        "(Tidiane) Sufi order, NOT the Mouride (Muridiyya) order. Never conflate them with Mouride "
        "beliefs, practices, history or figures (Cheikh Ahmadou Bamba, Touba, the Mouride brotherhood)."
    )
    examples = training_examples(question)
    if examples:
        system += "\n\nADMIN-APPROVED CORRECTIONS (use these as the authoritative answer when the question is similar; do not contradict them):\n"
        for question, answer in examples:
            system += f"Q: {question}\nA: {answer}\n\n"
    notes = feedback_notes(question)
    if notes:
        system += "\n\nUSER FEEDBACK AND CORRECTIONS (respect these reports; treat suggested answers as corrections):\n"
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
    candidate_answers = []
    if multi_model and len(selected_providers) > 1:
        from concurrent.futures import ThreadPoolExecutor

        def _run(p):
            return p, _call_provider(
                p, system, history, user_content, min(ask_settings.temperature, 0.2)
            )

        with ThreadPoolExecutor(max_workers=len(selected_providers)) as executor:
            results = list(executor.map(_run, selected_providers))
        for p, (ans, err) in results:
            if ans:
                candidate_answers.append((p.name, ans))

    if candidate_answers and len(candidate_answers) > 1:
        synthesis_system = (
            system
            + "\n\nYou are synthesizing answers from multiple AI models. "
            "Combine them into one accurate, well-structured answer. Prefer "
            "claims that are supported by the supplied evidence and agreed upon "
            "by multiple models. Remove any claim that is unsupported or "
            "contradicted by the evidence. Keep all [S#] citations."
        )
        synthesis_user = (
            user_content
            + "\n\nCANDIDATE ANSWERS FROM DIFFERENT MODELS:\n"
            + "\n\n".join(f"[{name}]\n{ans}" for name, ans in candidate_answers)
            + "\n\nProduce the final synthesized answer."
        )
        answer, error = _call_provider(
            provider,
            synthesis_system,
            history,
            synthesis_user,
            min(ask_settings.temperature, 0.2),
        )
    else:
        if candidate_answers:
            answer, error = candidate_answers[0][1], ""
        else:
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

    # Guard against hallucinated citations: keep only markers that point to
    # sources we actually retrieved.
    if answer:
        valid = {ref.get("citation") for ref in context["references"] if ref.get("citation")}
        import re as _re

        def _strip_unknown(match):
            number = match.group(1)
            if f"S{number}" in valid:
                return match.group(0)
            return ""

        answer = _re.sub(r"\\[S(\\d+)\\]", _strip_unknown, answer)

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
