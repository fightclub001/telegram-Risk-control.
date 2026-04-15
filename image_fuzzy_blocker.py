from __future__ import annotations

import io
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageOps


@dataclass(slots=True, frozen=True)
class ImageHashMatch:
    sample_id: int
    label: str
    ahash_distance: int
    dhash_distance: int

    @property
    def total_distance(self) -> int:
        return self.ahash_distance + self.dhash_distance


def _bits_to_int(bits: Iterable[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | (1 if bit else 0)
    return value


def _hamming_distance(left: int, right: int) -> int:
    return (int(left) ^ int(right)).bit_count()


def _open_image(image_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(image_bytes))
    return ImageOps.exif_transpose(image).convert("RGB")


def _average_hash(image: Image.Image, size: int = 8) -> int:
    gray = ImageOps.grayscale(image)
    reduced = gray.resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(reduced.getdata())
    avg = sum(int(px) for px in pixels) / max(1, len(pixels))
    return _bits_to_int(1 if int(px) >= avg else 0 for px in pixels)


def _difference_hash(image: Image.Image, width: int = 9, height: int = 8) -> int:
    gray = ImageOps.grayscale(image)
    reduced = gray.resize((width, height), Image.Resampling.LANCZOS)
    pixels = list(reduced.getdata())
    bits = []
    for y in range(height):
        base = y * width
        for x in range(width - 1):
            bits.append(1 if int(pixels[base + x]) <= int(pixels[base + x + 1]) else 0)
    return _bits_to_int(bits)


def build_image_hashes(image_bytes: bytes) -> tuple[int, int]:
    image = _open_image(image_bytes)
    return _average_hash(image), _difference_hash(image)


class ImageFuzzyBlocker:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.samples: list[dict[str, Any]] = []
        self._next_id = 1
        self.load()

    def load(self) -> None:
        try:
            if not os.path.exists(self.file_path):
                self.samples = []
                self._next_id = 1
                return
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raw = []
            cleaned: list[dict[str, Any]] = []
            max_id = 0
            for item in raw:
                if not isinstance(item, dict):
                    continue
                sample_id = int(item.get("id", 0) or 0)
                group_id = int(item.get("group_id", 0) or 0)
                label = str(item.get("label", "") or "").strip()
                ahash = int(item.get("ahash", 0) or 0)
                dhash = int(item.get("dhash", 0) or 0)
                if sample_id <= 0 or group_id == 0:
                    continue
                cleaned.append(
                    {
                        "id": sample_id,
                        "group_id": group_id,
                        "label": label,
                        "ahash": ahash,
                        "dhash": dhash,
                        "created_at": int(item.get("created_at", time.time()) or time.time()),
                    }
                )
                max_id = max(max_id, sample_id)
            self.samples = cleaned
            self._next_id = max_id + 1
        except Exception:
            self.samples = []
            self._next_id = 1

    def save(self) -> None:
        temp_path = f"{self.file_path}.tmp"
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.samples, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.file_path)

    def add_sample(self, *, group_id: int, label: str, image_bytes: bytes) -> dict[str, Any]:
        ahash, dhash = build_image_hashes(image_bytes)
        item = {
            "id": self._next_id,
            "group_id": int(group_id),
            "label": (label or "").strip(),
            "ahash": ahash,
            "dhash": dhash,
            "created_at": int(time.time()),
        }
        self.samples.append(item)
        self._next_id += 1
        self.save()
        return item

    def list_group_samples(self, group_id: int) -> list[dict[str, Any]]:
        items = [item for item in self.samples if int(item.get("group_id", 0) or 0) == int(group_id)]
        items.sort(key=lambda item: int(item.get("id", 0) or 0))
        return items

    def remove_samples(self, *, group_id: int, sample_ids: list[int]) -> list[int]:
        wanted = {int(sample_id) for sample_id in sample_ids if int(sample_id) > 0}
        if not wanted:
            return []
        kept: list[dict[str, Any]] = []
        removed: list[int] = []
        for item in self.samples:
            item_group = int(item.get("group_id", 0) or 0)
            item_id = int(item.get("id", 0) or 0)
            if item_group == int(group_id) and item_id in wanted:
                removed.append(item_id)
                continue
            kept.append(item)
        if removed:
            self.samples = kept
            self.save()
        removed.sort()
        return removed

    def check_image(
        self,
        *,
        group_id: int,
        image_bytes: bytes,
        max_total_distance: int = 12,
        max_single_distance: int = 8,
    ) -> ImageHashMatch | None:
        ahash, dhash = build_image_hashes(image_bytes)
        best: ImageHashMatch | None = None
        for sample in self.samples:
            if int(sample.get("group_id", 0) or 0) != int(group_id):
                continue
            ahash_distance = _hamming_distance(ahash, int(sample.get("ahash", 0) or 0))
            dhash_distance = _hamming_distance(dhash, int(sample.get("dhash", 0) or 0))
            total_distance = ahash_distance + dhash_distance
            if ahash_distance > max_single_distance or dhash_distance > max_single_distance:
                continue
            if total_distance > max_total_distance:
                continue
            current = ImageHashMatch(
                sample_id=int(sample.get("id", 0) or 0),
                label=str(sample.get("label", "") or ""),
                ahash_distance=ahash_distance,
                dhash_distance=dhash_distance,
            )
            if best is None or current.total_distance < best.total_distance:
                best = current
        return best
