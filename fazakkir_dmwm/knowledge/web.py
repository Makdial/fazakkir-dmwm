"""Keyless external research with an administrator-controlled allowlist."""

from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from django.conf import settings

from .models import SourceProfile, WebSourceRule

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; FazakkirBot/1.0; +https://fazakkirdmwm.com)"
HTTP_TIMEOUT = 9.0

_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
    "menu",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "source", "track", "wbr",
}


def _clean_search_url(value: str) -> str:
    value = html.unescape(value or "")
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            value = unquote(target[0])
            parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _rule_for(host: str, rules: list[WebSourceRule]) -> WebSourceRule | None:
    for rule in rules:
        domain = rule.domain.lower().strip().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return rule
    return None


def source_authority(url: str, rules: list[WebSourceRule] | None = None) -> str:
    rules = rules if rules is not None else list(WebSourceRule.objects.all())
    rule = _rule_for(_host(url), rules)
    return rule.authority if rule else SourceProfile.AUTHORITY_REFERENCE


def is_allowed(url: str, rules: list[WebSourceRule] | None = None) -> bool:
    """A domain is allowed when it has an explicit rule or is on the setting."""
    host = _host(url)
    if not host:
        return False
    rules = rules if rules is not None else list(WebSourceRule.objects.all())
    rule = _rule_for(host, rules)
    if rule is not None:
        return rule.is_approved
    allowed = getattr(settings, "AI_WEB_ALLOWED_DOMAINS", [])
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


class _DuckDuckGoParser(HTMLParser):
    """Read the result list from DuckDuckGo's no-JavaScript HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._mode: str | None = None
        self._depth = 0
        self._parts: list[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if self._mode:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._depth = 0
            self._parts = []
            self._href = attributes.get("href") or ""
        elif self.results and any(name.startswith("result__snippet") for name in classes):
            self._mode = "snippet"
            self._depth = 0
            self._parts = []

    def handle_endtag(self, tag):
        if not self._mode:
            return
        if self._depth > 0:
            self._depth -= 1
            return
        text = " ".join("".join(self._parts).split())
        if self._mode == "title":
            url = _clean_search_url(self._href)
            if text and url:
                self.results.append({"title": text[:200], "url": url, "snippet": ""})
        elif self.results:
            self.results[-1]["snippet"] = text[:700]
        self._mode = None

    def handle_data(self, data):
        if self._mode:
            self._parts.append(data)


class _PageTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip == 0:
            self.parts.append(data)


def _snippet(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: boundary + 1] if boundary > limit * 0.5 else cut.rsplit(" ", 1)[0] + "…"


def _wiki_results(question: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for language in ("en", "fr"):
        try:
            response = httpx.get(
                f"https://{language}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": question,
                    "gsrlimit": "2",
                    "prop": "extracts",
                    "explaintext": "1",
                    "exintro": "1",
                    "exchars": "2500",
                    "format": "json",
                    "formatversion": "2",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
            )
            if response.status_code != 200:
                continue
            pages = (response.json().get("query") or {}).get("pages") or []
            for page in pages[:2]:
                extract = (page.get("extract") or "").strip()
                title = (page.get("title") or "").strip()
                if not extract or not title:
                    continue
                url = (
                    f"https://{language}.wikipedia.org/wiki/"
                    f"{quote(title.replace(' ', '_'))}"
                )
                results.append(
                    {
                        "title": f"{title} (Wikipedia, {language})",
                        "url": url,
                        "snippet": _snippet(extract, 2300),
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("Wikipedia search failed for %r in %s", question, language, exc_info=True)
    return results


def _duck_results(question: str) -> list[dict[str, str]]:
    try:
        response = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": question},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return []
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        return parser.results[:8]
    except Exception:  # noqa: BLE001
        logger.debug("DuckDuckGo search failed for %r", question, exc_info=True)
        return []


def _fetch_page_text(url: str, fallback_title: str = "") -> dict[str, str]:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or "html" not in content_type:
            return {}
        parser = _PageTextParser()
        parser.feed(response.text[:1_500_000])
        text = " ".join(" ".join(parser.parts).split())
        if len(text) < 180:
            return {}
        title = " ".join(parser.title.split()) or fallback_title
        return {"title": title[:200], "url": str(response.url), "text": text[:6000]}
    except Exception:  # noqa: BLE001
        logger.debug("Could not read web page %s", url, exc_info=True)
        return {}


def research(question: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """Return approved external results with title, URL and useful text.

    Results from domains outside the allowlist are discarded before they ever
    reach the language model.  Wikipedia is used as a reliable fallback when a
    search engine is rate-limited.
    """
    rules = list(WebSourceRule.objects.all())
    wiki_results: list[dict[str, str]] = []
    duck_results: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        wiki_future = pool.submit(_wiki_results, question)
        duck_future = pool.submit(_duck_results, question)
        try:
            wiki_results = wiki_future.result(timeout=HTTP_TIMEOUT + 4)
        except Exception:  # noqa: BLE001
            logger.debug("Wikipedia research timed out", exc_info=True)
        try:
            duck_results = duck_future.result(timeout=HTTP_TIMEOUT + 4)
        except Exception:  # noqa: BLE001
            logger.debug("DuckDuckGo research timed out", exc_info=True)

    candidates = [
        result
        for result in wiki_results + duck_results
        if is_allowed(result["url"], rules)
    ]

    # Read the opening text of the first two approved search results, but keep
    # Wikipedia's extract: it is already a clean summary.
    page_candidates = [
        result for result in candidates if "wikipedia.org" not in _host(result["url"])
    ][:2]
    pages: list[dict[str, str]] = []
    if page_candidates:
        with ThreadPoolExecutor(max_workers=len(page_candidates)) as pool:
            futures = [
                pool.submit(_fetch_page_text, result["url"], result["title"])
                for result in page_candidates
            ]
            for future in futures:
                try:
                    page = future.result(timeout=HTTP_TIMEOUT + 4)
                except Exception:  # noqa: BLE001
                    page = {}
                if page:
                    pages.append(page)

    seen_urls = set()
    results: list[dict[str, str]] = []
    for result in candidates:
        if result["url"] in seen_urls:
            continue
        seen_urls.add(result["url"])
        result = dict(result)
        result["authority"] = source_authority(result["url"], rules)
        results.append(result)
        if len(results) >= max_results:
            break

    for page in pages:
        if page["url"] in seen_urls:
            continue
        if not is_allowed(page["url"], rules):
            continue
        results.append(
            {
                "title": page["title"],
                "url": page["url"],
                "snippet": _snippet(page["text"], 2600),
                "authority": source_authority(page["url"], rules),
            }
        )
        if len(results) >= max_results:
            break
    return results
