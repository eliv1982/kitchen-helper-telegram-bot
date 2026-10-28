"""Bytes that begin as pictures, for the photo tests. Nothing here is a decodable image, and
nothing in the application decodes one: it stores, shows and returns bytes, and tells the kind by
the signature the format starts with."""

import hashlib

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
WEBP_HEAD = b"RIFF\x24\x00\x00\x00WEBPVP8 "


def padded(head: bytes, size: int, seed: str = "") -> bytes:
    """`head` followed by deterministic filler, `size` bytes in all (distinct per seed)."""
    assert size >= len(head)
    out = bytearray(head)
    block = hashlib.sha256(seed.encode()).digest()
    while len(out) < size:
        out += block
        block = hashlib.sha256(block).digest()
    return bytes(out[:size])


def jpeg(size: int = 2048, seed: str = "") -> bytes:
    return padded(JPEG_HEAD, size, "jpeg" + seed)


def png(size: int = 2048, seed: str = "") -> bytes:
    return padded(PNG_HEAD, size, "png" + seed)


def webp(size: int = 2048, seed: str = "") -> bytes:
    return padded(WEBP_HEAD, size, "webp" + seed)


NOT_PICTURES = {
    "html": b"<html><script>alert(1)</script></html>" + b"\x00" * 20,
    "svg": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>' + b" " * 20,
    "gif": b"GIF89a" + b"\x00" * 30,
    "pdf": b"%PDF-1.7\n" + b"\x00" * 30,
    "text": "обычный текст, не картинка".encode("utf-8") * 3,
    "zip": b"PK\x03\x04" + b"\x00" * 30,
    "riff_but_wav": b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20,
    "jpeg_late": b"\x00" + JPEG_HEAD + b"\x00" * 20,
}
