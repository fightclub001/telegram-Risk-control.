from __future__ import annotations

"""
轻量级广告检测模块（替换原来的深度学习方案）

约束：
- 只能使用轻量依赖：simhash / rapidfuzz / sqlite3 / re / collections / time 等
- 不依赖 torch / transformers / sentence-transformers / faiss

接口保持与原先 SemanticAdDetector 一致：
- normalize_text(raw) -> str
- class SemanticAdDetector(data_dir):
    - add_ad_sample(raw_text) -> AdSample | None
    - list_samples() -> List[AdSample]
    - remove_sample(sample_id) -> bool
    - check_text(raw_text, min_len=4) -> (is_ad: bool, score: float, matched_id: Optional[int])
"""

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from rapidfuzz import fuzz  # type: ignore
from simhash import Simhash  # type: ignore


@dataclass(slots=True)
class AdSample:
    id: int
    text: str
    simhash: int
    fingerprint: List[str]
    created_at: float


_RE_KEEP = re.compile(r"[0-9a-z\u4e00-\u9fff]")
_RE_REPEAT = re.compile(r"(.)\1+")
DEFAULT_MATCH_THRESHOLD = float(os.getenv("SEMANTIC_AD_SCORE_THRESHOLD", "0.78"))
MAINTENANCE_INTERVAL_SEC = max(
    600, int((os.getenv("SEMANTIC_AD_MAINTENANCE_INTERVAL_SECONDS") or "21600").strip())
)
COMPACT_TEXT_RATIO = max(
    0.85, min(0.99, float((os.getenv("SEMANTIC_AD_COMPACT_TEXT_RATIO") or "0.92").strip()))
)
COMPACT_JACCARD_RATIO = max(
    0.60, min(0.95, float((os.getenv("SEMANTIC_AD_COMPACT_JACCARD_RATIO") or "0.72").strip()))
)
COMPACT_HAMMING_MAX = max(
    1, min(16, int((os.getenv("SEMANTIC_AD_COMPACT_HAMMING_MAX") or "6").strip()))
)


def normalize_text(raw: str) -> str:
    """文本归一化：只保留中英数字、小写、去标点空格、合并重复字符."""
    if not raw:
        return ""
    s = raw.lower()
    kept: List[str] = []
    for ch in s:
        if _RE_KEEP.match(ch):
            kept.append(ch)
    if not kept:
        return ""
    s2 = "".join(kept)
    s3 = _RE_REPEAT.sub(r"\1", s2)
    return s3


def _ngrams(text: str, n: int = 3) -> List[str]:
    if len(text) < n:
        return []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _jaccard(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return inter / union


def _containment_ratio(needle: List[str], haystack: List[str]) -> float:
    """Return how much of the smaller n-gram set is covered by the larger one."""
    if not needle or not haystack:
        return 0.0
    sn, sh = set(needle), set(haystack)
    if not sn:
        return 0.0
    return len(sn & sh) / len(sn)


def _simhash_from_tokens(tokens: List[str]) -> int:
    if not tokens:
        return 0
    return int(Simhash(tokens).value)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class SemanticAdDetector:
    """轻量级组合算法广告检测（SimHash + Ngram + RapidFuzz + 关键词权重）."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "semantic_ads.db")
        self._conn: Optional[sqlite3.Connection] = None
        self._last_maintenance_at = 0.0
        self._ensure_db()

    # ---------- DB ----------

    def _ensure_db(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=FILE")
        self._conn.execute("PRAGMA cache_size=-1024")
        self._conn.execute("PRAGMA mmap_size=0")
        self._conn.execute("PRAGMA cache_spill=ON")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                text_len INTEGER NOT NULL DEFAULT 0,
                simhash INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(ads)").fetchall()
        }
        if "text_len" not in columns:
            self._conn.execute("ALTER TABLE ads ADD COLUMN text_len INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        self._conn.execute(
            "UPDATE ads SET text_len = length(text) WHERE text_len IS NULL OR text_len <= 0"
        )
        self._conn.commit()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ads_created_at ON ads(created_at)"
        )
        self._conn.commit()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ads_text_len ON ads(text_len)"
        )
        self._conn.commit()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ads_text_unique ON ads(text)"
        )
        self._conn.commit()
        self._run_maintenance(force=True)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._ensure_db()
        assert self._conn is not None
        return self._conn

    def checkpoint(self) -> None:
        """在同步/备份前将 WAL 内容刷回主库，避免只复制到旧的 .db 快照。"""
        conn = self._get_conn()
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self.checkpoint()
        finally:
            self._conn.close()
            self._conn = None

    def _dedupe_existing_samples(self) -> int:
        """清理历史重复样本：相同 text 或相同 simhash 仅保留最早一条。"""
        conn = self._conn
        assert conn is not None
        cur = conn.execute(
            "SELECT id, text, simhash, text_len, created_at FROM ads ORDER BY created_at ASC, id ASC"
        )
        rows = cur.fetchall()
        seen_text: set[str] = set()
        seen_simhash: set[int] = set()
        delete_ids: List[int] = []
        for sid, text, simhash, _text_len, _created_at in rows:
            norm_text = str(text or "")
            sh = int(simhash)
            if norm_text in seen_text or sh in seen_simhash:
                delete_ids.append(int(sid))
                continue
            seen_text.add(norm_text)
            seen_simhash.add(sh)
        if not delete_ids:
            return 0
        conn.executemany("DELETE FROM ads WHERE id = ?", [(sid,) for sid in delete_ids])
        conn.commit()
        return len(delete_ids)

    def _compact_near_duplicate_samples(self) -> int:
        """
        周期性压缩高度相似样本，保留最早代表样本。

        目标不是“删除所有近义句”，而是清掉明显同模板、轻微改写、轻微插字的重复语料，
        避免广告库越积越大。
        """
        conn = self._conn
        assert conn is not None
        rows = conn.execute(
            """
            SELECT id, text, text_len, simhash, fingerprint, created_at
            FROM ads
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        if len(rows) <= 1:
            return 0

        buckets: dict[int, list[tuple[int, str, int, int, list[str]]]] = {}
        delete_ids: list[int] = []

        for sid, text, text_len, simhash, fp_str, _created_at in rows:
            sid = int(sid)
            norm_text = str(text or "")
            norm_len = int(text_len or len(norm_text) or 0)
            sh = int(simhash)
            grams = fp_str.split("|") if fp_str else _ngrams(norm_text, 3)
            bucket = max(1, norm_len // 6)

            candidates: list[tuple[int, str, int, int, list[str]]] = []
            for nearby_bucket in (bucket - 1, bucket, bucket + 1):
                if nearby_bucket <= 0:
                    continue
                candidates.extend(buckets.get(nearby_bucket, []))

            is_duplicate = False
            for _kept_id, kept_text, kept_len, kept_sh, kept_grams in candidates:
                max_len = max(norm_len, kept_len)
                if max_len <= 0:
                    continue
                if abs(norm_len - kept_len) > max(6, int(max_len * 0.35)):
                    continue
                if _hamming(sh, kept_sh) > COMPACT_HAMMING_MAX:
                    continue

                shorter_ratio = min(norm_len, kept_len) / max(1, max_len)
                if (
                    shorter_ratio >= 0.80
                    and (norm_text in kept_text or kept_text in norm_text)
                ):
                    is_duplicate = True
                    break

                text_ratio = fuzz.ratio(norm_text, kept_text) / 100.0
                if text_ratio < COMPACT_TEXT_RATIO:
                    continue
                jaccard_ratio = _jaccard(grams, kept_grams)
                if jaccard_ratio >= COMPACT_JACCARD_RATIO:
                    is_duplicate = True
                    break

            if is_duplicate:
                delete_ids.append(sid)
                continue

            buckets.setdefault(bucket, []).append((sid, norm_text, norm_len, sh, grams))

        if not delete_ids:
            return 0
        conn.executemany("DELETE FROM ads WHERE id = ?", [(sid,) for sid in delete_ids])
        conn.commit()
        return len(delete_ids)

    def _run_maintenance(self, force: bool = False) -> int:
        now = time.time()
        if not force and (now - self._last_maintenance_at) < MAINTENANCE_INTERVAL_SEC:
            return 0
        removed = self._dedupe_existing_samples()
        removed += self._compact_near_duplicate_samples()
        conn = self._get_conn()
        conn.execute("PRAGMA optimize")
        conn.commit()
        self._last_maintenance_at = now
        return removed

    # ---------- 广告样本管理 ----------

    def add_ad_sample(self, raw_text: str) -> Optional[AdSample]:
        """加入广告样本。只对“真正重复”的文本做去重，避免短句被误判为已存在。"""
        norm = normalize_text(raw_text)
        if not norm or len(norm) < 4:
            return None

        grams = _ngrams(norm, 3)
        if not grams:
            return None

        sh = _simhash_from_tokens(grams)
        if sh == 0:
            return None

        conn = self._get_conn()
        cur = conn.execute("SELECT id FROM ads WHERE text = ?", (norm,))
        if cur.fetchone() is not None:
            return None
        cur = conn.execute(
            """
            SELECT id, text, simhash, text_len
            FROM ads
            WHERE text_len BETWEEN ? AND ?
            """,
            (max(4, len(norm) - 1), len(norm) + 1),
        )
        rows = cur.fetchall()
        for sid, existing_text, existing_sh, existing_len in rows:
            existing_text = str(existing_text or "")
            d = _hamming(int(existing_sh), sh)
            # 对完全相同或几乎相同的样本才按 simhash 去重。
            # 否则短文本很容易发生误杀，表现为“转发学习了但实际没入库”。
            if (
                d <= 1
                and existing_text
                and abs(int(existing_len or len(existing_text)) - len(norm)) <= 1
                and fuzz.ratio(norm, existing_text) >= 98
            ):
                return None

        fp_str = "|".join(grams)
        now = time.time()
        try:
            cur = conn.execute(
                "INSERT INTO ads (text, text_len, simhash, fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",
                (norm, len(norm), str(sh), fp_str, now),
            )
        except sqlite3.IntegrityError:
            return None
        conn.commit()
        self._run_maintenance()
        new_id = int(cur.lastrowid)
        return AdSample(
            id=new_id,
            text=norm,
            simhash=sh,
            fingerprint=grams,
            created_at=now,
        )

    def list_samples(self) -> List[AdSample]:
        self._run_maintenance()
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, text, simhash, fingerprint, created_at FROM ads ORDER BY created_at ASC"
        )
        rows = cur.fetchall()
        samples: List[AdSample] = []
        for sid, text, sh, fp_str, ts in rows:
            grams = fp_str.split("|") if fp_str else []
            samples.append(
                AdSample(
                    id=int(sid),
                    text=str(text),
                    simhash=int(sh),
                    fingerprint=grams,
                    created_at=float(ts),
                )
            )
        return samples

    def remove_sample(self, sample_id: int) -> bool:
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM ads WHERE id = ?", (sample_id,))
        conn.commit()
        return cur.rowcount > 0

    # ---------- 新消息检测 ----------

    def check_text(self, raw_text: str, min_len: int = 4) -> Tuple[bool, float, Optional[int]]:
        """
        新消息检测：
        1. 归一化 + 文本长度过滤
        2. N-gram 指纹 + SimHash
        3. RapidFuzz 文本相似度
        4. 关键词权重
        5. 综合评分
        """
        norm = normalize_text(raw_text)
        if not norm or len(norm) < min_len:
            return False, 0.0, None

        self._run_maintenance()
        conn = self._get_conn()
        norm_len = len(norm)
        hard_rows = conn.execute(
            """
            SELECT id, text, text_len
            FROM ads
            WHERE text_len BETWEEN ? AND ?
            ORDER BY ABS(text_len - ?) ASC, text_len DESC, id ASC
            """,
            (min_len, max(norm_len + 24, int(norm_len * 2.5)), norm_len),
        )

        # 硬规则优先：
        # 1. 完全相同的归一化文案必须命中
        # 2. 新消息完整包含任一广告样本，也必须命中
        # 3. 已学习的长广告样本完整包含当前短变体时，也按高置信命中
        for sid, text_old, text_len in hard_rows:
            sample_text = str(text_old or "")
            if not sample_text:
                continue
            if norm == sample_text:
                return True, 1.0, int(sid)
            if sample_text in norm:
                return True, 1.0, int(sid)
            sample_len = int(text_len or len(sample_text))
            if (
                norm in sample_text
                and norm_len >= max(min_len + 2, int(sample_len * 0.45))
            ):
                return True, 0.96, int(sid)

        soft_min_len = max(min_len, int(norm_len * 0.35))
        soft_max_len = max(norm_len + 24, int(norm_len * 2.5))
        rows = conn.execute(
            """
            SELECT id, text, simhash, fingerprint
            FROM ads
            WHERE text_len BETWEEN ? AND ?
            ORDER BY text_len ASC, id ASC
            """,
            (soft_min_len, soft_max_len),
        )

        grams_new = _ngrams(norm, 3)
        sh_new = _simhash_from_tokens(grams_new if grams_new else [norm])

        best_score = 0.0
        best_id: Optional[int] = None

        for sid, text_old, sh_old, fp_str in rows:
            sid = int(sid)
            text_old = str(text_old)
            sh_old = int(sh_old)
            grams_old = fp_str.split("|") if fp_str else _ngrams(text_old, 3)

            ngram_sim = _jaccard(grams_new, grams_old)
            containment = max(
                _containment_ratio(grams_new, grams_old),
                _containment_ratio(grams_old, grams_new),
            )
            d = _hamming(sh_new, sh_old)
            simhash_similarity = 1.0 - d / 64.0

            partial_similarity = fuzz.partial_ratio(norm, text_old) / 100.0

            if (
                simhash_similarity <= 0.78
                and ngram_sim <= 0.60
                and containment <= 0.68
                and partial_similarity <= 0.88
            ):
                continue

            text_similarity = fuzz.ratio(norm, text_old) / 100.0

            keyword_weights = {
                "免费": 0.2,
                "下载": 0.2,
                "破解": 0.3,
                "频道": 0.2,
                "福利": 0.1,
                "tg": 0.2,
                "加群": 0.2,
            }
            keyword_score = 0.0
            for kw, w in keyword_weights.items():
                if kw in raw_text or kw in norm:
                    keyword_score += w
            if keyword_score > 1.0:
                keyword_score = 1.0

            balanced_score = (
                0.35 * simhash_similarity
                + 0.30 * text_similarity
                + 0.20 * ngram_sim
                + 0.15 * keyword_score
            )
            containment_score = (
                0.38 * partial_similarity
                + 0.27 * containment
                + 0.20 * ngram_sim
                + 0.15 * keyword_score
            )
            score = max(balanced_score, containment_score)

            # Learned ad text should catch minor rewrites and sliced variants.
            if (
                partial_similarity >= 0.92
                and containment >= 0.72
                and max(norm_len, len(text_old)) >= 8
            ):
                score = max(score, 0.90)
            elif (
                partial_similarity >= 0.88
                and containment >= 0.62
                and keyword_score >= 0.2
            ):
                score = max(score, 0.84)

            if score > best_score:
                best_score = score
                best_id = sid

        is_ad = best_score >= DEFAULT_MATCH_THRESHOLD and best_id is not None
        return is_ad, best_score, best_id

