from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageOps, ImageStat

from join_approval_text_normalizer import count_chinese_chars, normalize_text


@dataclass(slots=True, frozen=True)
class AvatarResult:
    extracted_text: str
    normalized_text: str
    is_text_avatar: bool
    chinese_char_count: int
    total_char_count: int
    matched_term: str | None = None


@dataclass(slots=True, frozen=True)
class PrefilterStats:
    looks_like_text: bool
    contrast_std: float
    top_ratio: float
    top2_ratio: float
    reduced_top2_ratio: float
    edge_density: float
    palette_size: int


class JoinApprovalAvatarOCR:
    """
    Lightweight join approval OCR wrapper.

    Main process never imports RapidOCR/ONNX/OpenCV directly.
    OCR runs in a short-lived subprocess only when a user actually has a profile photo.
    """

    def __init__(self) -> None:
        self.ocr_enabled = (os.getenv("OCR_ENABLED") or "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.ocr_max_side = max(64, int((os.getenv("OCR_MAX_SIDE") or "320").strip()))
        self.prefilter_enabled = (os.getenv("OCR_PREFILTER_ENABLED") or "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.prefilter_max_side = max(48, int((os.getenv("OCR_PREFILTER_MAX_SIDE") or "96").strip()))
        self.fail_closed_on_prefilter_hit = (os.getenv("OCR_FAIL_CLOSED_ON_PREFILTER_HIT") or "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    def _prefilter_stats(self, image_bytes: bytes) -> PrefilterStats:
        """
        Cheap prefilter for join approval.

        Most normal user avatars are photos/illustrations with richer color distribution.
        Text-heavy porn-spam avatars are usually poster-like: few dominant colors + strong edges.
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((self.prefilter_max_side, self.prefilter_max_side), Image.Resampling.BILINEAR)
        except Exception:
            return PrefilterStats(True, 0.0, 1.0, 1.0, 1.0, 1.0, 0)

        width, height = image.size
        if width < 24 or height < 24:
            return PrefilterStats(True, 0.0, 1.0, 1.0, 1.0, 1.0, 0)

        total = width * height
        gray = ImageOps.grayscale(image)
        gray_stat = ImageStat.Stat(gray)
        contrast_std = float(gray_stat.stddev[0] or 0.0)
        if contrast_std < 18.0:
            return PrefilterStats(False, contrast_std, 0.0, 0.0, 0.0, 0.0, 8)

        quantized = image.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        colors = quantized.getcolors(maxcolors=total) or []
        colors.sort(reverse=True, key=lambda item: item[0])
        top_ratio = float(colors[0][0]) / total if colors else 0.0
        top2_ratio = float(sum(count for count, _color in colors[:2])) / total if colors else 0.0
        palette_size = len(colors)
        reduced = image.quantize(colors=3, method=Image.Quantize.MEDIANCUT)
        reduced_colors = reduced.getcolors(maxcolors=total) or []
        reduced_colors.sort(reverse=True, key=lambda item: item[0])
        reduced_top2_ratio = (
            float(sum(count for count, _color in reduced_colors[:2])) / total if reduced_colors else 0.0
        )

        pixels = gray.load()
        edge_hits = 0
        edge_total = 0
        for y in range(height):
            for x in range(width):
                current = int(pixels[x, y])
                if x + 1 < width:
                    edge_total += 1
                    if abs(current - int(pixels[x + 1, y])) >= 42:
                        edge_hits += 1
                if y + 1 < height:
                    edge_total += 1
                    if abs(current - int(pixels[x, y + 1])) >= 42:
                        edge_hits += 1
        edge_density = float(edge_hits) / max(1, edge_total)

        looks_like_text = False
        if top_ratio >= 0.46 and edge_density >= 0.060 and contrast_std >= 24.0:
            looks_like_text = True
        elif top2_ratio >= 0.76 and edge_density >= 0.048 and contrast_std >= 22.0:
            looks_like_text = True
        elif palette_size <= 4 and edge_density >= 0.055 and contrast_std >= 20.0:
            looks_like_text = True
        elif reduced_top2_ratio >= 0.70 and edge_density >= 0.050 and contrast_std >= 20.0:
            looks_like_text = True
        elif reduced_top2_ratio >= 0.66 and edge_density >= 0.072 and contrast_std >= 22.0:
            looks_like_text = True

        return PrefilterStats(
            looks_like_text=looks_like_text,
            contrast_std=contrast_std,
            top_ratio=top_ratio,
            top2_ratio=top2_ratio,
            reduced_top2_ratio=reduced_top2_ratio,
            edge_density=edge_density,
            palette_size=palette_size,
        )

    def _looks_like_text_avatar(self, image_bytes: bytes) -> bool:
        return self._prefilter_stats(image_bytes).looks_like_text

    def analyze_avatar(self, image_bytes: bytes) -> AvatarResult:
        if not self.ocr_enabled:
            return AvatarResult("", "", False, 0, 0, None)
        prefilter = self._prefilter_stats(image_bytes)
        if self.prefilter_enabled and not prefilter.looks_like_text:
            return AvatarResult("", "", False, 0, 0, None)

        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".img") as tmp:
                tmp.write(image_bytes)
                temp_path = tmp.name

            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__), temp_path, str(self.ocr_max_side)],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "avatar ocr subprocess failed")
            raw = json.loads(proc.stdout.strip() or "{}")
            return AvatarResult(
                extracted_text=str(raw.get("extracted_text", "")),
                normalized_text=str(raw.get("normalized_text", "")),
                is_text_avatar=bool(raw.get("is_text_avatar", False)),
                chinese_char_count=int(raw.get("chinese_char_count", 0)),
                total_char_count=int(raw.get("total_char_count", 0)),
                matched_term=None,
            )
        except Exception:
            if self.fail_closed_on_prefilter_hit and prefilter.looks_like_text:
                return AvatarResult("", "", True, 0, 0, None)
            raise
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def _worker_process(image_path: str, max_side: int) -> int:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr_onnxruntime import RapidOCR

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    image = image.convert("L")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    image = ImageOps.autocontrast(image)
    image = image.point(lambda px: 255 if px > 170 else 0)

    engine = RapidOCR()
    result, _ = engine(np.asarray(image))

    extracted_text = ""
    if result:
        extracted_text = " ".join(
            str(item[1]).strip()
            for item in result
            if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[1]).strip()
        )

    normalized_text = normalize_text(extracted_text)
    total_char_count = len(normalized_text)
    chinese_char_count = count_chinese_chars(normalized_text)

    # Reject clearly textual avatars:
    # - 2+ Chinese chars, or
    # - 4+ normalized alnum chars (English/number text avatars)
    is_text_avatar = chinese_char_count >= 2 or total_char_count >= 4

    print(
        json.dumps(
            {
                "extracted_text": extracted_text,
                "normalized_text": normalized_text,
                "is_text_avatar": is_text_avatar,
                "chinese_char_count": chinese_char_count,
                "total_char_count": total_char_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    raise SystemExit(_worker_process(sys.argv[1], int(sys.argv[2])))
