"""Extension-vs-content checks shared by every upload path.

Chat attachments (:mod:`app.services.attachment_service`) have always sniffed the
first bytes of a stored file and rejected an upload whose content contradicts its
extension. Knowledge-base documents and artifacts did not: they checked the
extension against a whitelist and stopped there, so ``evil.pdf`` containing an
executable, a script, or simply the wrong format was persisted, indexed and later
offered back for download under that name.

This module is the single copy of that table + logic, so a new upload path gets
the same behaviour for free instead of re-implementing (and drifting from) it.
Two design notes:

  * The sniffer is *purely* extension-vs-bytes: it raises :class:`FileSignatureError`
    and knows nothing about HTTP. Each caller maps it onto its own error envelope
    (attachments keep their ``attachment_*`` codes, documents get ``document_*``),
    so this stays free of any API-layer dependency.
  * Text formats (``.txt``/``.md``/``.csv``/``.json``) have no magic bytes and are
    accepted on extension alone — that is deliberate, not a gap.
"""
from __future__ import annotations

import os

__all__ = [
    "EXT_RULES",
    "FileSignatureError",
    "accepted_mimes_for",
    "check_file_signature",
    "verified_size",
]

# Legacy Office (.doc/.xls/.ppt) is the OLE2 compound file container; Word also
# happily opens / saves RTF under a .doc name, and LibreOffice (our .doc parser)
# accepts that too. Anything else under those extensions is a mismatch.
_OFFICE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_RTF_MAGIC = b"{\\rtf"

# Extension -> (accepted MIME prefixes, magic-byte signatures). Signatures are
# matched against the first bytes of the saved file. Text types have no robust
# signature and are accepted on extension + MIME alone.
EXT_RULES: dict[str, tuple[tuple[str, ...], tuple[bytes, ...]]] = {
    ".pdf":  (("application/pdf",), (b"%PDF",)),
    ".png":  (("image/png",), (b"\x89PNG\r\n\x1a\n",)),
    ".jpg":  (("image/jpeg",), (b"\xff\xd8\xff",)),
    ".jpeg": (("image/jpeg",), (b"\xff\xd8\xff",)),
    ".webp": (("image/webp", "image/riff"), (b"RIFF",)),
    ".gif":  (("image/gif",), (b"GIF87a", b"GIF89a")),
    ".bmp":  (("image/bmp", "image/x-ms-bmp"), (b"BM",)),
    ".tif":  (("image/tiff",), (b"II*\x00", b"MM\x00*")),
    ".tiff": (("image/tiff",), (b"II*\x00", b"MM\x00*")),
    ".docx": (("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".xlsx": (("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".odt":  (("application/vnd.oasis.opendocument.text",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".ods":  (("application/vnd.oasis.opendocument.spreadsheet",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".odp":  (("application/vnd.oasis.opendocument.presentation",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".pptx": (("application/vnd.openxmlformats-officedocument.presentationml.presentation",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    # Legacy Office: the parser chain (LibreOffice / xlrd) needs a real container,
    # so require the OLE2 magic (or RTF for Word documents) instead of trusting
    # the extension.
    ".doc":  (("application/msword", "application/vnd.ms-office",
               "application/CDFV2", "application/rtf", "text/rtf",
               "application/octet-stream"),
              (_OFFICE2_MAGIC, _RTF_MAGIC)),
    ".xls":  (("application/vnd.ms-excel", "application/vnd.ms-office",
               "application/CDFV2", "application/octet-stream"),
              (_OFFICE2_MAGIC,)),
    ".ppt":  (("application/vnd.ms-powerpoint", "application/vnd.ms-office",
               "application/CDFV2", "application/octet-stream"),
              (_OFFICE2_MAGIC,)),
    ".txt":  (("text/plain", "text/markdown", "application/octet-stream"), ()),
    ".md":   (("text/markdown", "text/plain", "application/octet-stream"), ()),
    ".csv":  (("text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"), ()),
    ".json": (("application/json", "text/plain", "application/octet-stream"), ()),
}

# Bytes read from the head of the file: enough for every signature above (the
# longest is the 8-byte OLE2 container magic) without reading a whole document.
_HEAD_BYTES = 16


class FileSignatureError(Exception):
    """A stored file's bytes contradict the extension it was saved under.

    ``reason`` is a stable machine token (``unreadable`` | ``mismatch``) so each
    call site can pick its own public error code; ``message`` is the user-facing
    Chinese text.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def accepted_mimes_for(ext: str) -> tuple[str, ...]:
    """MIME types a browser may legitimately declare for ``ext``."""
    rule = EXT_RULES.get(ext)
    return rule[0] if rule else ()


def check_file_signature(path: str, ext: str) -> None:
    """Raise :class:`FileSignatureError` unless ``path``'s head matches ``ext``.

    Extensions we have no rule for (and text rules with no signatures) pass: the
    whitelist that gated the upload already decided the type is acceptable, and
    there is no reliable magic to test.
    """
    rule = EXT_RULES.get(ext)
    if rule is None:
        return
    _mimes, sigs = rule
    if not sigs:
        return
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError as exc:
        raise FileSignatureError("unreadable", "无法读取上传文件") from exc
    if not any(head.startswith(s) for s in sigs):
        raise FileSignatureError(
            "mismatch", "文件内容与其扩展名声明不一致"
        )


def verified_size(path: str) -> int:
    """Byte size of a stored file, or 0 when it cannot be stat'ed.

    Upload handlers used to trust ``UploadFile.size`` (None until the framework
    has buffered the body, so the guard silently skipped) — the on-disk size after
    a streamed save is the only number that reflects what was actually written.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
