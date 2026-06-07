"""web_search / read_url backends - provider-agnostic, stdlib only.

Mirrors the models.py provider seam: one normalized result shape regardless of
backend, picked by config.

  search:      DuckDuckGo (no key, default) | Serper (Google SERP, keyed upgrade)
  extraction:  Jina Reader (r.jina.ai, no key; a key only lifts rate limits)

A result is a dict {title, url, snippet}. The factories return async callables
the dispatcher injects into the Toolbox; the sim injects fakes instead, so no
test ever hits the network. stdlib HTTP runs in a thread so the event loop never
blocks. Discord-agnostic; pure ASCII.

DDG is best-effort (it throttles a busy single IP - that's the whole reason
Serper is the recommended path for a heavy fleet). New providers (tavily, brave,
exa) drop in as one more branch in `make_searcher` with no caller change.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (compatible; cddc-agent/1.0)"
_TIMEOUT = 20


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read(400_000).decode("utf-8", "replace")


# --- search providers ------------------------------------------------------
def _ddg(query: str, num: int) -> list[dict]:
    """DuckDuckGo HTML endpoint scrape - no key. Throttles a busy IP; Serper is
    the recommended upgrade for fleet-scale use."""
    import html as _html
    import re

    body = _http(
        "https://html.duckduckgo.com/html/",
        data=urllib.parse.urlencode({"q": query}).encode(),
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    out: list[dict] = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        body, re.S,
    ):
        url = _html.unescape(m.group("url"))
        # DDG wraps the target in a redirect: //duckduckgo.com/l/?uddg=<enc>
        parts = urllib.parse.urlparse(url)
        if parts.path.endswith("/l/"):
            uddg = urllib.parse.parse_qs(parts.query).get("uddg")
            if uddg:
                url = uddg[0]
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group("title"))).strip()
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= num:
            break
    snips = [
        _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    ]
    for i, s in enumerate(snips[: len(out)]):
        out[i]["snippet"] = s
    return out


def _serper(query: str, num: int, api_key: str) -> list[dict]:
    """Serper.dev Google SERP - keyed. Clean JSON; the recommended heavy path."""
    body = _http(
        "https://google.serper.dev/search",
        data=json.dumps({"q": query, "num": num}).encode(),
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    data = json.loads(body)
    out: list[dict] = []
    for item in (data.get("organic") or [])[:num]:
        out.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return out


def _jina(url: str, api_key: str) -> str:
    """Jina Reader - clean LLM-friendly text for a PUBLIC page. No key needed; a
    key lifts rate limits. Prepends r.jina.ai/ to the target."""
    target = url if url.startswith(("http://", "https://")) else "https://" + url
    headers = {"User-Agent": _UA, "Accept": "text/plain"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return _http("https://r.jina.ai/" + target, headers=headers, timeout=30)


# --- factories: return async callables the Toolbox calls -------------------
def make_searcher(provider: str, *, serper_key: str = "", num_results: int = 5):
    """async (query, num=None) -> list[dict], or None if web search is disabled."""
    provider = (provider or "").strip().lower()
    if provider in ("", "none", "off"):
        return None

    async def search(query: str, num: int | None = None) -> list[dict]:
        n = max(1, min(int(num or num_results), 10))

        def _run() -> list[dict]:
            if provider == "serper":
                if not serper_key:
                    raise RuntimeError(
                        "CDDC_WEB_SEARCH=serper but SERPER_API_KEY is empty"
                    )
                return _serper(query, n, serper_key)
            return _ddg(query, n)  # ddg is the default / fallback

        return await asyncio.to_thread(_run)

    return search


def make_reader(*, jina_key: str = ""):
    """async (url) -> str clean-text reader (Jina). Always available (no key)."""

    async def read(url: str) -> str:
        return await asyncio.to_thread(_jina, url, jina_key)

    return read
