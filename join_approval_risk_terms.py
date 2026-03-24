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


class JoinApprovalRiskTerms:
    """Fast normalized keyword matcher for join approval."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.opencc_config = (os.getenv("OPENCC_CONFIG") or "t2s").strip()
        self.terms = self._load_terms()
        self.pattern = self._build_pattern(self.terms)

    def _load_terms(self) -> tuple[str, ...]:
        terms = list(DEFAULT_RISK_TERMS)
        extra_terms = (os.getenv("EXTRA_TERMS") or "").strip()
        if extra_terms:
            terms.extend(item.strip() for item in extra_terms.split(",") if item.strip())

        extra_file = self.base_dir / "extra_terms.txt"
        if extra_file.exists():
            terms.extend(
                line.strip()
                for line in extra_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )

        out: list[str] = []
        seen: set[str] = set()
        for term in terms:
            normalized = normalize_text(term, self.opencc_config)
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        out.sort(key=len, reverse=True)
        return tuple(out)

    @staticmethod
    def _build_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
        if not terms:
            return re.compile(r"$^")
        return re.compile("|".join(re.escape(term) for term in terms))

    def match(self, text: str) -> str | None:
        normalized = normalize_text(text, self.opencc_config)
        if not normalized:
            return None
        matched = self.pattern.search(normalized)
        if not matched:
            return None
        return matched.group(0)
