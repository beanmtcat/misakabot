from __future__ import annotations

import re
import unicodedata

from .domain import NormalizedMessage

URL_RE = re.compile(r"(?:(?:https?://|tg://)[^\s<>]+|(?:t\.me|telegram\.me)/[^\s<>]+)", re.I)
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,})")
INVISIBLE_CATEGORIES = {"Cf", "Mn"}
# Include U+2027 (‧), a common "fire text" separator used to split keywords.
SEPARATOR_RE = re.compile(r"[\s._·•‧|｜~～\-—–]+")


def _compact_text(text: str) -> tuple[str, bool]:
    normalized_unicode = unicodedata.normalize("NFKC", text).lower()
    had_invisible_chars = any(unicodedata.category(char) in INVISIBLE_CATEGORIES for char in normalized_unicode)
    without_invisible = "".join(char for char in normalized_unicode if unicodedata.category(char) not in INVISIBLE_CATEGORIES)
    return SEPARATOR_RE.sub("", without_invisible), had_invisible_chars


def normalize_message(text: str | None, sender_name: str | None = None) -> NormalizedMessage:
    raw_text = text or ""
    raw_sender_name = sender_name or ""
    urls = tuple(URL_RE.findall(raw_text))
    mentions = tuple(f"@{match}" for match in MENTION_RE.findall(raw_text))
    normalized_text, text_had_invisible_chars = _compact_text(raw_text)
    normalized_sender_name, sender_had_invisible_chars = _compact_text(raw_sender_name)
    return NormalizedMessage(
        raw_text=raw_text,
        normalized_text=normalized_text,
        urls=urls,
        mentions=mentions,
        had_invisible_chars=text_had_invisible_chars or sender_had_invisible_chars,
        sender_name_raw=raw_sender_name,
        normalized_sender_name=normalized_sender_name,
    )
