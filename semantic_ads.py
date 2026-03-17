import os
import json
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

import faiss  # type: ignore
import numpy as np
from sentence_transformers import SentenceTransformer  # type: ignore


@dataclass
class AdSample:
    id: int
    text: str
    vector: List[float]
    timestamp: float


def normalize_text(raw: str) -> str:
    """多阶段广告检测 - 第一阶段：文本归一化."""
    if not raw:
        return ""
    # 统一为小写
    s = raw.lower()
    # 只保留中文、字母、数字
    filtered_chars: List[str] = []
    for ch in s:
        # 中文
        if "\u4e00" <= ch <= "\u9fff":
            filtered_chars.append(ch)
            continue
        # 英文
        if "a" <= ch <= "z":
            filtered_chars.append(ch)
            continue
        # 数字
        if "0" <= ch <= "9":
            filtered_chars.append(ch)
            continue
        # 其他全部丢弃（包括符号、空格等）
    if not filtered_chars:
        return ""
    # 删除连续重复字符
    result = [filtered_chars[0]]
    for ch in filtered_chars[1:]:
        if ch != result[-1]:
            result.append(ch)
    return "".join(result)


class SemanticAdDetector:
    """多阶段广告检测系统（语义向量 + FAISS 检索）."""

    def __init__(self, data_dir: str, model_name: str = "shibing624/text2vec-base-chinese") -> None:
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "semantic_ads.json")
        self.index_path = os.path.join(self.data_dir, "semantic_ads.index")
        self.model_name = model_name

        self._model: Optional[SentenceTransformer] = None
        self._index: Optional[faiss.IndexFlatIP] = None
        self._dim: int = 768
        self._next_id: int = 1
        self._samples: List[AdSample] = []

        self._load_state()

    # ---------- 模型与索引 ----------

    def _ensure_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _ensure_index(self) -> faiss.IndexFlatIP:
        if self._index is None:
            self._index = faiss.IndexFlatIP(self._dim)
            if self._samples:
                vecs = np.array([s.vector for s in self._samples], dtype="float32")
                self._index.add(vecs)
        return self._index

    # ---------- 状态持久化 ----------

    def _load_state(self) -> None:
        # 加载广告样本
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._samples = [
                    AdSample(
                        id=it["id"],
                        text=it["text"],
                        vector=it["vector"],
                        timestamp=it.get("timestamp", time.time()),
                    )
                    for it in raw
                ]
                if self._samples:
                    self._dim = len(self._samples[0].vector)
                    self._next_id = max(s.id for s in self._samples) + 1
            except Exception:
                self._samples = []
                self._next_id = 1

        # 加载 FAISS 索引（如果存在）
        if os.path.exists(self.index_path):
            try:
                self._index = faiss.read_index(self.index_path)
                self._dim = self._index.d
            except Exception:
                self._index = None

    def _save_state(self) -> None:
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump([asdict(s) for s in self._samples], f, ensure_ascii=False)
        except Exception:
            # 持久化失败不影响在线检测
            pass

        if self._index is not None:
            try:
                faiss.write_index(self._index, self.index_path)
            except Exception:
                pass

    # ---------- 查询与维护 ----------

    def list_samples(self) -> List[AdSample]:
        """返回当前广告样本列表（按时间排序，新的在后）."""
        return sorted(self._samples, key=lambda s: s.timestamp)

    def remove_sample(self, sample_id: int) -> bool:
        """删除指定广告样本，并重建索引."""
        idx = None
        for i, s in enumerate(self._samples):
            if s.id == sample_id:
                idx = i
                break
        if idx is None:
            return False
        self._samples.pop(idx)

        # 重建索引
        if self._samples:
            self._dim = len(self._samples[0].vector)
            self._index = faiss.IndexFlatIP(self._dim)
            vecs = np.array([s.vector for s in self._samples], dtype="float32")
            self._index.add(vecs)
        else:
            self._index = faiss.IndexFlatIP(self._dim)

        self._save_state()
        return True

    # ---------- 语义向量 ----------

    def _encode(self, text: str) -> np.ndarray:
        """第二阶段：生成语义向量，并做 L2 归一化."""
        model = self._ensure_model()
        emb = model.encode(text, normalize_embeddings=True)
        if emb.ndim == 1:
            return emb.astype("float32")
        return emb[0].astype("float32")

    # ---------- 广告样本管理 ----------

    def add_ad_sample(self, raw_text: str) -> Optional[AdSample]:
        """管理员删除消息时调用：写入广告样本库（带相似度去重）."""
        norm = normalize_text(raw_text)
        if not norm:
            return None

        vec = self._encode(norm)

        # 去重检测：与现有广告最高相似度 >= 0.995 则视为重复，不再入库
        if self._samples:
            sim, _ = self._search_single(vec)
            if sim is not None and sim >= 0.995:
                return None

        sample = AdSample(
            id=self._next_id,
            text=norm,
            vector=vec.tolist(),
            timestamp=time.time(),
        )
        self._next_id += 1
        self._samples.append(sample)

        index = self._ensure_index()
        index.add(vec.reshape(1, -1))

        self._save_state()
        return sample

    # ---------- 相似度搜索 ----------

    def _search_single(self, vec: np.ndarray) -> Tuple[Optional[float], Optional[int]]:
        """在广告库中搜索最近邻，返回 (similarity, sample_id)."""
        if not self._samples:
            return None, None

        index = self._ensure_index()
        # FAISS 接口：输入 shape (n, d)
        D, I = index.search(vec.reshape(1, -1), 1)
        sim = float(D[0][0])
        idx = int(I[0][0])
        if idx < 0 or idx >= len(self._samples):
            return None, None
        return sim, self._samples[idx].id

    # ---------- 新消息检测 ----------

    def check_text(self, raw_text: str, min_len: int = 4) -> Tuple[bool, float, Optional[int]]:
        """
        新消息检测：
        1. 归一化
        2. 文本长度过滤
        3. 生成语义向量
        4. 向量相似度搜索
        5. 阈值判定

        返回: (is_ad, similarity, matched_sample_id)
        """
        norm = normalize_text(raw_text)
        if not norm or len(norm) < min_len:
            return False, 0.0, None

        vec = self._encode(norm)
        sim, sid = self._search_single(vec)
        if sim is None:
            return False, 0.0, None

        # 最终判定条件
        is_ad = sim >= 0.98
        return is_ad, sim, sid

