from __future__ import annotations

import os
import re
import unicodedata
from functools import lru_cache

from opencc import OpenCC


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_KEEP_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")


@lru_cache(maxsize=4)
def _get_converter(config_name: str) -> OpenCC:
    return OpenCC(config_name)


def normalize_text(text: str, opencc_config: str | None = None) -> str:
    """Fast normalization for join approval checks."""
    if not text:
        return ""

    config_name = opencc_config or os.getenv("OPENCC_CONFIG", "t2s")
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"}
    )
    normalized = _get_converter(config_name).convert(normalized)
    parts = _KEEP_RE.findall(normalized)
    return "".join(parts)


def count_chinese_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
