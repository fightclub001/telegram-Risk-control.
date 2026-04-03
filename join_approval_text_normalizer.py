from __future__ import annotations

import re
import unicodedata


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_KEEP_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")


def normalize_text(text: str) -> str:
    """Normalize OCR text for lightweight text-avatar detection."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"}
    )
    return "".join(_KEEP_RE.findall(normalized))


def count_chinese_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
