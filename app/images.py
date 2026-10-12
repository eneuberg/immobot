"""Image helper shared by the LLM (Fredy's single image) and the listing galleries (media.py):
decode foreign image data safely, shrink it and re-encode it as JPEG."""

import io

from PIL import Image

MAX_PIXELS = 40_000_000  # refuse decompression bombs before decoding
MAX_SIDE = 1024
JPEG_QUALITY = 85


def to_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """Any image Pillow can read (JPEG, PNG, WebP, ...) -> RGB JPEG of at most max_side px. Raises on garbage."""
    with Image.open(io.BytesIO(data)) as src:
        if src.width * src.height > MAX_PIXELS:
            raise ValueError("image has too many pixels")
        src.draft("RGB", (max_side, max_side))  # cheap downscale while decoding JPEGs
        img = src.convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()
