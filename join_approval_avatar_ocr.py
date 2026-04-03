from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from join_approval_text_normalizer import count_chinese_chars, normalize_text


@dataclass(slots=True, frozen=True)
class AvatarResult:
    extracted_text: str
    normalized_text: str
    is_text_avatar: bool
    chinese_char_count: int
    total_char_count: int
    matched_term: str | None = None


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

    def analyze_avatar(self, image_bytes: bytes) -> AvatarResult:
        if not self.ocr_enabled:
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
