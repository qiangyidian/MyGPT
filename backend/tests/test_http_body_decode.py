"""http_get 正文解码（回归：gzip 乱码烧穿 run 的 token 预算）。

生产事故 2026-10-11：Web Researcher 对一个返回 gzip HTML 的 URL 直接把压缩
字节当成 utf-8 文本塞回上下文，\x1f\x8b 开头的乱码体数千 token，把一次多 Agent
检索的 prompt 推到 45k、撞了 40k 的预算墙。根因是 ``aiter_raw`` 不做内容解码
（httpx 的 auto-decompress 只在 ``aiter_bytes`` 里）。

这里钉三条线：gzip/deflate 要解出来、按 Content-Type 拒收二进制、
utf-8 解不动的「假文本」(mostly-replacement) 也拒收。
"""
from __future__ import annotations

import gzip
import zlib

from app.tools.builtin import _decode_http_body


def test_gzip_html_is_decompressed_not_mangled():
    page = "<html><body>{}</body></html>".format("正文" * 500)
    raw = gzip.compress(page.encode("utf-8"))

    out = _decode_http_body(raw, "text/html; charset=UTF-8", 4000)

    assert out is not None
    assert "<html>" in out


def test_magic_gzip_bytes_decode_even_without_content_type():
    raw = gzip.compress(b"real text payload")

    out = _decode_http_body(raw, "", 4000)

    assert out == "real text payload"


def test_deflate_body_is_inflated():
    raw = zlib.compress(b"deflated payload")

    out = _decode_http_body(raw, "application/deflate", 4000)

    assert out == "deflated payload"


def test_image_content_type_is_refused():
    out = _decode_http_body(b"\x89PNG fake pixels", "image/png", 4000)

    assert out is None


def test_binary_garbage_without_text_integrity_is_refused():
    # 解不回 utf-8、且大多数是 replacement 字节的二进制体——旧实现会把它
    # 全量塞给模型。
    raw = bytes(range(256)) * 4

    out = _decode_http_body(raw, "", 4000)

    assert out is None


def test_plain_text_survives_and_truncates():
    out = _decode_http_body("你好世界".encode() * 100, "text/plain", 40)

    assert out is not None
    assert len(out) <= 40
    assert out.startswith("你好")


def test_corrupt_gzip_is_refused_not_decompressed():
    out = _decode_http_body(b"\x1f\x8b definitely not really gzip", "text/html", 4000)

    assert out is None
