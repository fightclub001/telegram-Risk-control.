from __future__ import annotations

import io
import json
import os
import time
import math
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageOps


@dataclass(slots=True, frozen=True)
class ImageHashMatch:
    sample_id: int
    label: str
    ahash_distance: int
    dhash_distance: int
    phash_distance: int | None = None
    matched_hashes: int = 2

    @property
    def total_distance(self) -> int:
        distances = [self.ahash_distance, self.dhash_distance]
        if self.phash_distance is not None:
            distances.append(self.phash_distance)
            return sum(sorted(distances)[:2])
        return sum(distances)


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


def _dct_1d(values: list[float]) -> list[float]:
    size = len(values)
    result = [0.0] * size
    factor = math.pi / (2.0 * size)
    scale0 = math.sqrt(1.0 / size)
    scale = math.sqrt(2.0 / size)
    for k in range(size):
        acc = 0.0
        for n, value in enumerate(values):
            acc += value * math.cos((2 * n + 1) * k * factor)
        result[k] = acc * (scale0 if k == 0 else scale)
    return result


def _perceptual_hash(image: Image.Image, size: int = 32, low_freq_size: int = 8) -> int:
    gray = ImageOps.grayscale(image)
    reduced = gray.resize((size, size), Image.Resampling.LANCZOS)
    matrix = [
        [float(reduced.getpixel((x, y))) for x in range(size)]
        for y in range(size)
    ]
    row_dct = [_dct_1d(row) for row in matrix]
    coeffs = [[0.0] * size for _ in range(size)]
    for x in range(size):
        column = [row_dct[y][x] for y in range(size)]
        transformed = _dct_1d(column)
        for y in range(size):
            coeffs[y][x] = transformed[y]
    values = []
    for y in range(low_freq_size):
        for x in range(low_freq_size):
            if x == 0 and y == 0:
                continue
            values.append(coeffs[y][x])
    median = sorted(values)[len(values) // 2] if values else 0.0
    return _bits_to_int(1 if value >= median else 0 for value in values)


def build_image_hashes(image_bytes: bytes) -> dict[str, int]:
    image = _open_image(image_bytes)
    return {
        "ahash": _average_hash(image),
        "dhash": _difference_hash(image),
        "phash": _perceptual_hash(image),
    }


def match_image_hashes(
    *,
    sample_id: int,
    label: str,
    sample_hashes: dict[str, int],
    candidate_hashes: dict[str, int],
    max_total_distance: int = 12,
    max_single_distance: int = 8,
) -> ImageHashMatch | None:
    ahash_distance = _hamming_distance(
        int(candidate_hashes.get("ahash", 0) or 0),
        int(sample_hashes.get("ahash", 0) or 0),
    )
    dhash_distance = _hamming_distance(
        int(candidate_hashes.get("dhash", 0) or 0),
        int(sample_hashes.get("dhash", 0) or 0),
    )
    phash_distance: int | None = None
    sample_has_phash = "phash" in sample_hashes and int(sample_hashes.get("phash", 0) or 0) > 0
    candidate_has_phash = "phash" in candidate_hashes and int(candidate_hashes.get("phash", 0) or 0) > 0
    if sample_has_phash and candidate_has_phash:
        phash_distance = _hamming_distance(
            int(candidate_hashes.get("phash", 0) or 0),
            int(sample_hashes.get("phash", 0) or 0),
        )
        distances = [ahash_distance, dhash_distance, phash_distance]
        close_count = sum(1 for distance in distances if distance <= max_single_distance)
        if close_count < 2:
            return None
        total_distance = sum(sorted(distances)[:2])
        if total_distance > max_total_distance:
            return None
        return ImageHashMatch(
            sample_id=sample_id,
            label=label,
            ahash_distance=ahash_distance,
            dhash_distance=dhash_distance,
            phash_distance=phash_distance,
            matched_hashes=close_count,
        )

    total_distance = ahash_distance + dhash_distance
    if ahash_distance > max_single_distance or dhash_distance > max_single_distance:
        return None
    if total_distance > max_total_distance:
        return None
    return ImageHashMatch(
        sample_id=sample_id,
        label=label,
        ahash_distance=ahash_distance,
        dhash_distance=dhash_distance,
        phash_distance=phash_distance,
        matched_hashes=2,
    )


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
                        "phash": int(item.get("phash", 0) or 0),
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
        hashes = build_image_hashes(image_bytes)
        item = {
            "id": self._next_id,
            "group_id": int(group_id),
            "label": (label or "").strip(),
            "ahash": int(hashes["ahash"]),
            "dhash": int(hashes["dhash"]),
            "phash": int(hashes["phash"]),
            "created_at": int(time.time()),
        }
        self.samples.append(item)
        self._next_id += 1
        self.save()
        return item

    def build_hashes(self, image_bytes: bytes) -> dict[str, int]:
        return build_image_hashes(image_bytes)

    def match_candidate_hashes(
        self,
        sample: dict[str, Any],
        candidate_hashes: dict[str, int],
        *,
        max_total_distance: int = 12,
        max_single_distance: int = 8,
    ) -> ImageHashMatch | None:
        return match_image_hashes(
            sample_id=int(sample.get("id", 0) or 0),
            label=str(sample.get("label", "") or ""),
            sample_hashes={
                "ahash": int(sample.get("ahash", 0) or 0),
                "dhash": int(sample.get("dhash", 0) or 0),
                "phash": int(sample.get("phash", 0) or 0),
            },
            candidate_hashes=candidate_hashes,
            max_total_distance=max_total_distance,
            max_single_distance=max_single_distance,
        )

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
        candidate_hashes = build_image_hashes(image_bytes)
        best: ImageHashMatch | None = None
        for sample in self.samples:
            if int(sample.get("group_id", 0) or 0) != int(group_id):
                continue
            current = self.match_candidate_hashes(
                sample,
                candidate_hashes,
                max_total_distance=max_total_distance,
                max_single_distance=max_single_distance,
            )
            if current is None:
                continue
            if (
                best is None
                or current.total_distance < best.total_distance
                or (
                    current.total_distance == best.total_distance
                    and current.matched_hashes > best.matched_hashes
                )
            ):
                best = current
        return best
