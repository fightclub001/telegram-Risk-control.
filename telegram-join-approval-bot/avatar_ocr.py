from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

from settings import Settings
from risk_terms import RiskTermsMatcher
from text_normalizer import count_chinese_chars, normalize_text


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class AvatarResult:
    extracted_text: str
    normalized_text: str
    is_text_avatar: bool
    chinese_char_count: int
    total_char_count: int
    matched_term: str | None


class AvatarOCR:
    """Single-pass OCR analyzer for profile photos."""

    def __init__(self, settings: Settings, matcher: RiskTermsMatcher) -> None:
        self.settings = settings
        self.matcher = matcher
        self._engine = RapidOCR() if settings.ocr_enabled else None

    def analyze_avatar(self, image_bytes: bytes) -> AvatarResult:
        """Run lightweight OCR and text-avatar checks."""
        if not self.settings.ocr_enabled or self._engine is None:
            return AvatarResult("", "", False, 0, 0, None)

        image = Image.open(io.BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image = self._preprocess(image)

        ocr_input = np.asarray(image)
        result, _ = self._engine(ocr_input)

        extracted_text = ""
        if result:
            extracted_text = " ".join(
                str(item[1]).strip()
                for item in result
                if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[1]).strip()
            )

        normalized_text = normalize_text(extracted_text, self.settings.opencc_config)
        if not normalized_text:
            return AvatarResult(extracted_text, "", False, 0, 0, None)

        total_char_count = len(normalized_text)
        chinese_char_count = count_chinese_chars(normalized_text)
        ratio = chinese_char_count / total_char_count if total_char_count else 0.0
        is_text_avatar = chinese_char_count >= 2 and ratio >= 0.7
        matched_term = self.matcher.match(normalized_text) if is_text_avatar else None

        return AvatarResult(
            extracted_text=extracted_text,
            normalized_text=normalized_text,
            is_text_avatar=is_text_avatar,
            chinese_char_count=chinese_char_count,
            total_char_count=total_char_count,
            matched_term=matched_term,
        )

    def _preprocess(self, image: Image.Image) -> Image.Image:
        image = image.convert("L")
        image.thumbnail(
            (self.settings.ocr_max_side, self.settings.ocr_max_side),
            Image.Resampling.LANCZOS,
        )
        image = ImageOps.autocontrast(image)
        image = image.point(lambda px: 255 if px > 170 else 0)
        return image
