"""Native web extract — local httpx fetch + trafilatura content extraction.

Restores the ``native`` extract backend that was removed from Hermes
(``_is_backend_available("native")`` had no case, so ``web.extract_backend:
native`` silently fell through to whichever search backend was configured —
SearXNG is search-only, so every ``web_extract`` call errored with
"SearXNG is a search-only backend"). This plugin implements it for real:
no API key, no paid subscription, fully local.

Extract-only — pair with a search provider (searxng, ddgs, brave-free) for
``web_search`` calls. ``supports_search()`` returns False.

Config keys this provider responds to::

    web:
      extract_backend: "native"     # explicit per-capability
      backend: "native"             # shared fallback

No env vars required. Always available as long as ``trafilatura`` is
importable in the active venv (``uv pip install trafilatura``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import async_is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB cap so a huge page can't blow up memory/context
_TIMEOUT_S = 20.0
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Hermes/native-extract"
)


def _trafilatura_importable() -> bool:
    try:
        import trafilatura  # noqa: F401

        return True
    except ImportError:
        return False


async def _fetch(url: str) -> Dict[str, Any]:
    """Fetch one URL and return raw HTML + final (post-redirect) URL, or an error dict."""
    import httpx

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > _MAX_BYTES:
                return {"error": f"Page too large ({content_length} bytes, cap is {_MAX_BYTES})"}

            body = resp.content[:_MAX_BYTES]
            return {"html": body.decode(resp.encoding or "utf-8", errors="replace"), "final_url": str(resp.url)}
    except httpx.TimeoutException:
        return {"error": f"Fetch timed out after {_TIMEOUT_S:.0f}s"}
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code} fetching {url}"}
    except httpx.RequestError as exc:
        return {"error": f"Could not fetch {url}: {exc}"}


def _extract_sync(html: str, final_url: str, output_format: str) -> Dict[str, Any]:
    import trafilatura

    text = trafilatura.extract(
        html,
        url=final_url,
        output_format=output_format,
        favor_recall=True,
        include_links=True,
        include_tables=True,
    )
    meta = trafilatura.extract_metadata(html, default_url=final_url)
    return {
        "text": text or "",
        "title": (meta.title if meta else "") or "",
        "author": (meta.author if meta else "") or "",
        "date": (meta.date if meta else "") or "",
    }


class NativeWebExtractProvider(WebSearchProvider):
    """Extract via a local httpx fetch + trafilatura boilerplate-stripping parse."""

    @property
    def name(self) -> str:
        return "native"

    @property
    def display_name(self) -> str:
        return "Native (local, no key)"

    def is_available(self) -> bool:
        return _trafilatura_importable()

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        from tools.interrupt import is_interrupted as _is_interrupted

        if _is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        fmt = kwargs.get("format")
        output_format = "html" if fmt == "html" else "markdown"

        results: List[Dict[str, Any]] = []
        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "error": "Interrupted", "title": ""})
                continue

            blocked = check_website_access(url)
            if blocked:
                logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": blocked["message"],
                        "blocked_by_policy": {
                            "host": blocked["host"],
                            "rule": blocked["rule"],
                            "source": blocked["source"],
                        },
                    }
                )
                continue

            fetched = await _fetch(url)
            if "error" in fetched:
                results.append({"url": url, "title": "", "content": "", "error": fetched["error"]})
                continue

            final_url = fetched["final_url"]

            # Re-check SSRF + policy on the post-redirect URL, matching the
            # firecrawl provider's redirect-aware gate.
            if final_url != url:
                if not await async_is_safe_url(final_url):
                    results.append(
                        {
                            "url": final_url,
                            "title": "",
                            "content": "",
                            "error": "Blocked: redirect target is a private or internal network address",
                        }
                    )
                    continue
                final_blocked = check_website_access(final_url)
                if final_blocked:
                    logger.info(
                        "Blocked redirected web_extract for %s by rule %s",
                        final_blocked["host"],
                        final_blocked["rule"],
                    )
                    results.append(
                        {
                            "url": final_url,
                            "title": "",
                            "content": "",
                            "error": final_blocked["message"],
                            "blocked_by_policy": {
                                "host": final_blocked["host"],
                                "rule": final_blocked["rule"],
                                "source": final_blocked["source"],
                            },
                        }
                    )
                    continue

            try:
                parsed = await asyncio.to_thread(_extract_sync, fetched["html"], final_url, output_format)
            except Exception as exc:  # noqa: BLE001 — a parse-library edge case must not crash the tool call
                logger.warning("trafilatura extraction failed for %s: %s", final_url, exc)
                results.append({"url": final_url, "title": "", "content": "", "error": f"Content extraction failed: {exc}"})
                continue

            results.append(
                {
                    "url": final_url,
                    "title": parsed["title"],
                    "content": parsed["text"],
                    "raw_content": fetched["html"][:_MAX_BYTES],
                    "metadata": {"author": parsed["author"], "date": parsed["date"]},
                }
            )

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Native (local, no key)",
            "badge": "free · local",
            "tag": "Local httpx fetch + trafilatura extraction. No account, no API key, nothing leaves your machine.",
            "env_vars": [],
        }
