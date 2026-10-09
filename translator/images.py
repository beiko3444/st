"""Bounded clipboard image uploads; never accept client URLs or file paths."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DATA_LENGTH = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64


@dataclass(frozen=True)
class ImageAttachment:
    data: bytes
    extension: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def parse_image(value: str) -> ImageAttachment:
    if len(value) > MAX_IMAGE_DATA_LENGTH:
        raise ValueError("이미지는 8MB 이하로 붙여넣어 주세요.")
    header, separator, encoded = value.partition(",")
    formats = {
        "data:image/png;base64": ".png",
        "data:image/jpeg;base64": ".jpg",
        "data:image/webp;base64": ".webp",
    }
    extension = formats.get(header)
    if not separator or extension is None:
        raise ValueError("PNG, JPG, WebP 이미지만 번역할 수 있습니다.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("이미지를 읽을 수 없습니다. 다시 붙여넣어 주세요.") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("이미지는 8MB 이하로 붙여넣어 주세요.")
    matches = {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33,
        ".jpg": data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
        ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 20,
    }
    if not matches[extension]:
        raise ValueError("이미지 형식이 올바르지 않습니다. 다시 붙여넣어 주세요.")
    return ImageAttachment(data, extension)
