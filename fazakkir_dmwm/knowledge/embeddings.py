"""Embedding back ends for the knowledge index.

OpenAI's embedding endpoint gives true cross-lingual semantic search when a key
is configured.  Without a key the site must still work, so a deterministic local
hashing embedding is used instead.  It captures shared vocabulary and Arabic
morphology well enough for keyword-style recall, but it is not a substitute for
a neural embedding model; the dashboard shows which provider is active.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_STOPWORDS = frozenset(
    """
    a an the and or but if in on at to of for with from by is are was were be
    been being it its this that these those as not no yes do does did have has
    had i you he she we they me my your his her our their
    what who whom whose how why when where which tell about please explain
    can could would should say says said according regarding between into than
    then there here also just more most some any all very much many such
    au aux avec ce ces cet cette dans de des du elle elles en est et il ils je
    la le les leur lui mais mes moi mon ne nos notre nous on ou par pas pour
    que qui quoi quel quelle quels quelles comment pourquoi quand où sans sous
    sur ta te tes toi ton tu un une vos votre vous plus moins tout tous toute
    toutes comme aussi bien peut peu très même autre autres son sa ses
    ما ماذا من في على عن إلى الى هو هي هم هن أنا انت أنت نحن كان كانت يكون
    هذا هذه ذلك تلك التي الذي الذين اللذين اللاتي أو او ثم لا لم لن قد كل بعض
    أي اي كيف لماذا متى أين اين هل نعم بين عند حتى إذا اذا كما لكن بل إن ان
    أن
    """.split()
)
_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال", "و", "ب", "ل", "ك", "ف")
_SUFFIXES = ("هما", "ها", "هم", "كم", "كن", "نا", "ات", "ون", "ين", "ان", "ة", "ه", "ي")


def normalise_text(value: str) -> str:
    """Normalise Arabic and Latin text for matching and hashing."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().replace("ـ", "")
    value = (
        value.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ى", "ي")
        .replace("ئ", "ي")
        .replace("ؤ", "و")
        .replace("ة", "ه")
    )
    value = _ARABIC_DIACRITICS.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _stem(token: str) -> str:
    if not token:
        return token
    for prefix in _PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            token = token[len(prefix) :]
            break
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    return token


def search_tokens(value: str) -> list[str]:
    tokens = []
    for raw in _TOKEN_RE.findall(normalise_text(value)):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        token = _stem(raw)
        if len(token) >= 2:
            tokens.append(token)
    return tokens


def _hash_vector(text: str, dimension: int) -> list[float]:
    vector = [0.0] * dimension
    tokens = search_tokens(text)
    features: list[tuple[str, float]] = [(token, 1.0) for token in tokens]

    # Character trigrams make related Arabic forms (e.g. صبر / صابر) share
    # dimensions even when the light stemmer does not reduce them to one form.
    for token in tokens:
        if len(token) >= 4:
            for index in range(len(token) - 2):
                features.append((f"#{token[index:index + 3]}", 0.35))

    for feature, weight in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dimension
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def local_embedding(text: str, dimension: int = 384) -> list[float]:
    return _hash_vector(text, dimension)


def local_model_name(dimension: int = 384) -> str:
    return f"local-hash-{dimension}"


def local_dimension(model_name: str, fallback: int = 384) -> int:
    try:
        return int((model_name or "").rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return fallback


def _openai_embeddings(texts: list[str]) -> list[list[float]]:
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    model = getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    dimensions = int(getattr(settings, "EMBEDDING_DIMENSIONS", 384))
    vectors: list[list[float]] = []
    batch_size = 48

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        payload = {"model": model, "input": batch}
        if model.startswith("text-embedding-3"):
            payload["dimensions"] = dimensions
        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=90.0,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Embedding provider returned {response.status_code}: {response.text[:300]}"
            )
        data = response.json().get("data") or []
        if len(data) != len(batch):
            raise RuntimeError("Embedding provider returned an unexpected batch size")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors.extend([list(item.get("embedding") or []) for item in ordered])

    return vectors


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str, str]:
    """Embed a batch and return ``(vectors, provider, model)``."""
    configured = (getattr(settings, "EMBEDDING_PROVIDER", "auto") or "auto").lower()
    dimensions = int(getattr(settings, "EMBEDDING_DIMENSIONS", 384))
    model = getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")

    if configured in ("auto", "openai"):
        try:
            return _openai_embeddings(texts), "openai", model
        except Exception:  # noqa: BLE001
            if configured == "openai":
                logger.warning(
                    "OpenAI embeddings failed; falling back to local embeddings",
                    exc_info=True,
                )
            else:
                logger.debug("OpenAI embeddings unavailable", exc_info=True)

    return (
        [local_embedding(text, dimensions) for text in texts],
        "local",
        local_model_name(dimensions),
    )


def embed_query(text: str, provider: str, model: str) -> list[float] | None:
    """Embed a query with the same provider used by the stored chunks."""
    if provider == "openai":
        try:
            return _openai_embeddings([text])[0]
        except Exception:  # noqa: BLE001
            logger.warning("Could not embed the search query", exc_info=True)
            return None
    dimension = local_dimension(model, int(getattr(settings, "EMBEDDING_DIMENSIONS", 384)))
    return local_embedding(text, dimension)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    return sum(left[index] * right[index] for index in range(size))
