"""Keyword-based skill auto-trigger scanner.

Reads ``triggers:`` frontmatter lists from SKILL.md files and prepends
matching skill content to the user message before the agent sees it.
This file is written and maintained by ~/bin/hermes-patch.py.
"""
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_triggers_loaded_at: float = 0.0
_cached_triggers: list = []
_TRIGGER_CACHE_TTL = 30.0  # seconds between re-scans of SKILL.md files


def _load_triggers() -> list:
    """Return list of (cmd_key, [trigger_phrase, ...]) for all enabled skills."""
    try:
        from tools.skills_tool import (
            SKILLS_DIR,
            _parse_frontmatter,
            skill_matches_platform,
            _get_disabled_skill_names,
        )
        from agent.skill_utils import get_external_skills_dirs, iter_skill_index_files

        disabled = _get_disabled_skill_names()
        result = []
        seen_names: set = set()

        dirs_to_scan = []
        if SKILLS_DIR.exists():
            dirs_to_scan.append(SKILLS_DIR)
        dirs_to_scan.extend(get_external_skills_dirs())

        for scan_dir in dirs_to_scan:
            for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
                if any(p in {".git", ".github", ".hub", ".archive"} for p in skill_md.parts):
                    continue
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    frontmatter, _ = _parse_frontmatter(content)
                    if not skill_matches_platform(frontmatter):
                        continue
                    name = frontmatter.get("name", skill_md.parent.name)
                    if name in seen_names or name in disabled:
                        continue
                    triggers = frontmatter.get("triggers", [])
                    if not triggers or not isinstance(triggers, list):
                        continue
                    seen_names.add(name)
                    cmd_name = name.lower().replace(" ", "-").replace("_", "-")
                    cmd_name = re.sub(r"[^a-z0-9-]", "", cmd_name)
                    cmd_name = re.sub(r"-{2,}", "-", cmd_name).strip("-")
                    if cmd_name:
                        result.append((f"/{cmd_name}", [str(t) for t in triggers if t]))
                except Exception:
                    continue
        return result
    except Exception:
        return []


def _get_triggers() -> list:
    global _triggers_loaded_at, _cached_triggers
    now = time.monotonic()
    if now - _triggers_loaded_at > _TRIGGER_CACHE_TTL:
        _cached_triggers = _load_triggers()
        _triggers_loaded_at = now
    return _cached_triggers


def scan_triggers(message: str) -> list:
    """Return cmd_keys for skills whose triggers substring-match the message."""
    if not message or message.startswith(_SKILL_INVOCATION_PREFIX):
        return []
    msg_lower = message.lower()
    matched = []
    for cmd_key, triggers in _get_triggers():
        for phrase in triggers:
            if phrase.lower() in msg_lower:
                matched.append(cmd_key)
                break
    return matched


def auto_trigger(message: str, task_id: Optional[str] = None) -> str:
    """Prepend matched skill content to the message. Returns the augmented string.

    Skips messages that are already skill invocations. Safe to call on every
    user turn — returns the original message unchanged when nothing matches.
    """
    if not message or not isinstance(message, str):
        return message
    if message.startswith(_SKILL_INVOCATION_PREFIX):
        return message
    matched_keys = scan_triggers(message)
    if not matched_keys:
        return message
    try:
        from agent.skill_commands import build_skill_invocation_message

        parts = []
        for cmd_key in matched_keys:
            skill_msg = build_skill_invocation_message(
                cmd_key,
                user_instruction=message,
                task_id=task_id,
                runtime_note="Auto-triggered by keyword match.",
            )
            if skill_msg:
                parts.append(skill_msg)
                logger.debug("Trigger scanner: auto-loaded skill %s", cmd_key)
        if parts:
            return "\n\n---\n\n".join(parts)
    except Exception as exc:
        logger.debug("Trigger scanner error: %s", exc)
    return message
