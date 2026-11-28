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

from .models import AskInteraction, AskSettings
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
    """Pick the configured chat provider, preferring Groq's free tier."""
    groq_key = (
        getattr(settings, "GROQ_API_KEY", "")
        or os.environ.get("GROQ_API_KEY", "")
    ).strip()
    openai_key = (
        getattr(settings, "OPENAI_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    ).strip()

    if groq_key:
        return ChatProvider(
            name="groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            key=groq_key,
            model=os.environ.get("GROQ_ASK_MODEL", "openai/gpt-oss-20b"),
        )
    if openai_key:
        return ChatProvider(
            name="openai",
            url="https://api.openai.com/v1/chat/completions",
            key=openai_key,
            model=os.environ.get("OPENAI_ASK_MODEL", "gpt-4o-mini"),
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
            "- If the answer is not in the evidence, say so."
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
        ask_settings.temperature,
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
