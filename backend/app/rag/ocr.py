"""OCR backends for image / scanned-page text extraction.

Two pluggable backends, selected by ``settings.OCR_ENGINE``:

  * ``rapidocr`` (default) — ``rapidocr-onnxruntime`` ships its own ONNX models
    and installs via pip with no system binary. Cross-platform; the sensible
    default, especially on Windows where provisioning a system Tesseract is
    awkward.
  * ``tesseract`` — ``pytesseract`` wraps a system-installed Tesseract binary
    (must itself be on PATH). Lightweight at runtime, heavier to provision.

Both expose :func:`image_to_text`, which accepts a filesystem path, raw image
bytes, or a ``PIL.Image``. OCR is **best-effort**: any backend failure returns
``""`` so a scanned page or photo never hard-fails a document parse. Models are
loaded lazily and cached per process — the first OCR call pays the one-time
model-load cost; subsequent calls reuse the engine.
"""
from __future__ import annotations

import io
import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# A path, raw bytes, or a PIL.Image instance.
ImageInput = str | bytes | bytearray | Any

_engine: Any | None = None
_engine_kind: str | None = None


def _to_pil(image: ImageInput):
    """Coerce path / bytes / PIL.Image into a (downscaled) PIL.Image.

    Oversized images are downscaled to ``OCR_MAX_IMAGE_EDGE`` first: OCR cost
    scales super-linearly with pixel count, and a full-screen screenshot on CPU
    can blow the attachment parse timeout outright. 1568px is rapidocr's native
    detection resolution, so the downscale costs no accuracy.
    """
    from PIL import Image  # lazy

    if isinstance(image, (bytes, bytearray)):
        pil = Image.open(io.BytesIO(image))
    elif isinstance(image, str):
        pil = Image.open(image)
    else:
        pil = image  # already a PIL image
    max_edge = get_settings().OCR_MAX_IMAGE_EDGE
    if max_edge > 0:
        pil.thumbnail((max_edge, max_edge))
    return pil


def _to_ndarray(image: ImageInput):
    """rapidocr wants a path or an ``np.ndarray`` — coerce into the latter."""
    import numpy as np  # lazy

    pil = _to_pil(image)
    return np.array(pil.convert("RGB"))


def _get_engine() -> tuple[Any | None, str | None]:
    """Lazily build + cache the OCR backend chosen by ``OCR_ENGINE``.

    Falls back rapidocr -> tesseract -> None (OCR disabled). A disabled engine
    is not fatal: callers get "" and the document still parses without OCR.
    """
    global _engine, _engine_kind
    if _engine is not None or _engine_kind == "__failed__":
        return _engine, None if _engine_kind == "__failed__" else _engine_kind

    kind = (get_settings().OCR_ENGINE or "rapidocr").strip().lower()
    if kind == "tesseract":
        try:
            import pytesseract  # noqa: F401  — presence check only

            _engine, _engine_kind = "tesseract", "tesseract"
            return _engine, _engine_kind
        except Exception as exc:
            logger.warning("pytesseract 不可用，回退到 rapidocr: %s", exc)
            kind = "rapidocr"

    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
        _engine_kind = "rapidocr"
    except Exception as exc:
        logger.warning("rapidocr 不可用，OCR 已禁用: %s", exc)
        _engine, _engine_kind = None, "__failed__"
        return None, None
    return _engine, _engine_kind


def _parse_rapidocr_result(result: Any) -> str:
    """Normalize rapidocr's per-version return shapes into plain text.

    * 1.x: ``(list[[box, text, score], ...], elapse)`` or ``None``
    * 2.x: an object exposing ``.txts``
    """
    if not result:
        return ""
    # 2.x-style object with .txts
    txts = getattr(result, "txts", None)
    if txts:
        return "\n".join(t for t in txts if t)
    # 1.x-style tuple
    rows = result[0] if isinstance(result, tuple) else result
    texts: list[str] = []
    if rows:
        for item in rows:
            try:
                texts.append(item[1])
            except (IndexError, TypeError):
                continue
    return "\n".join(t for t in texts if t)


def image_to_text(image: ImageInput) -> str:
    """Best-effort OCR of a single image; ``""`` on any failure / no engine."""
    engine, kind = _get_engine()
    if engine is None:
        return ""
    try:
        if kind == "tesseract":
            import pytesseract  # lazy

            return pytesseract.image_to_string(_to_pil(image)) or ""
        # rapidocr accepts a path directly, else feed it an ndarray.
        payload = image if isinstance(image, str) else _to_ndarray(image)
        return _parse_rapidocr_result(engine(payload))
    except Exception as exc:
        logger.debug("OCR failed for one image: %s", exc)
        return ""


def is_available() -> bool:
    """True if at least one OCR backend could be initialised."""
    engine, _ = _get_engine()
    return engine is not None
