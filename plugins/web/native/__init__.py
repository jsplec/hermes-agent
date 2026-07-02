"""Native web extract plugin — bundled, auto-loaded.

Local httpx fetch + trafilatura extraction. No API key, nothing leaves the
machine. Extract-only — pair with a search provider (searxng/ddgs/brave-free)
for web_search calls.
"""

from __future__ import annotations

from plugins.web.native.provider import NativeWebExtractProvider


def register(ctx) -> None:
    """Register the native extract provider with the plugin context."""
    ctx.register_web_search_provider(NativeWebExtractProvider())
