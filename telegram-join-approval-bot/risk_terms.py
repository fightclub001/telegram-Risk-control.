from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from settings import Settings
from text_normalizer import normalize_text


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


class RiskTermsMatcher:
    """Normalized risk term matcher with optional env/file extensions."""

    def __init__(self, settings: Settings, base_dir: Path) -> None:
        self.settings = settings
        self.base_dir = base_dir
        self.terms = self._load_terms()
        self.pattern = self._build_pattern(self.terms)

    def _load_terms(self) -> tuple[str, ...]:
        terms: list[str] = list(DEFAULT_RISK_TERMS)

        if self.settings.extra_terms:
            terms.extend(
                item.strip()
                for item in self.settings.extra_terms.split(",")
                if item.strip()
            )

        extra_file = self.base_dir / self.settings.extra_terms_file
        if extra_file.exists():
            terms.extend(
                line.strip()
                for line in extra_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )

        normalized_terms: list[str] = []
        seen: set[str] = set()
        for term in terms:
            normalized = normalize_text(term, self.settings.opencc_config)
            if len(normalized) < 2:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            normalized_terms.append(normalized)
        normalized_terms.sort(key=len, reverse=True)
        return tuple(normalized_terms)

    @staticmethod
    def _build_pattern(terms: Iterable[str]) -> re.Pattern[str]:
        escaped = [re.escape(term) for term in terms]
        if not escaped:
            return re.compile(r"$^")
        return re.compile("|".join(escaped))

    def match(self, text: str) -> str | None:
        """Return the matched normalized term if any."""
        normalized = normalize_text(text, self.settings.opencc_config)
        if not normalized:
            return None
        matched = self.pattern.search(normalized)
        if not matched:
            return None
        return matched.group(0)
