from __future__ import annotations

import os
import re
from pathlib import Path

from join_approval_text_normalizer import normalize_text


DEFAULT_RISK_TERMS: tuple[str, ...] = (
    "幼女",
    "呦女",
    "父女",
    "童车",
    "幼童",
    "女儿",
    "呦呦",
    "未成年",
    "简介",
    "点我",
    "处女",
    "萝莉",
    "看我",
    "头像",
)


def normalize_terms(terms: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized_terms: list[str] = []
    for term in terms:
        normalized = normalize_text(term)
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        normalized_terms.append(normalized)
    normalized_terms.sort(key=len, reverse=True)
    return tuple(normalized_terms)


def match_terms(text: str, terms: list[str] | tuple[str, ...]) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    for term in normalize_terms(terms):
        if term and term in normalized:
            return term
    return None


class JoinApprovalRiskMatcher:
    """Lightweight normalized keyword matcher for OCR avatar text."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)
        self.terms = self._load_terms()
        escaped = [re.escape(term) for term in self.terms]
        self.pattern = re.compile("|".join(escaped)) if escaped else re.compile(r"$^")

    def _load_terms(self) -> tuple[str, ...]:
        raw_terms: list[str] = list(DEFAULT_RISK_TERMS)
        extra_terms_env = (os.getenv("EXTRA_TERMS") or "").strip()
        if extra_terms_env:
            raw_terms.extend(item.strip() for item in extra_terms_env.split(",") if item.strip())

        extra_terms_file = self.base_dir / "extra_terms.txt"
        if extra_terms_file.exists():
            try:
                raw_terms.extend(
                    line.strip()
                    for line in extra_terms_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except Exception:
                pass

        return normalize_terms(raw_terms)

    def match(self, text: str) -> str | None:
        normalized = normalize_text(text)
        if not normalized:
            return None
        matched = self.pattern.search(normalized)
        if not matched:
            return None
        return matched.group(0)
