"""Small, policy-neutral helpers for turning external bytes into text."""

from __future__ import annotations

import codecs
import os
import sys
from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class DecodedText:
    """Text plus enough provenance to make decoding loss visible."""

    text: str
    codec: str
    bom: bytes = b""
    had_errors: bool = False
    loss: bool = False
    source: str = ""


@dataclass(frozen=True, slots=True)
class ByteRangeText:
    """A decoded byte range and trailing bytes held for its next chunk."""

    decoded: DecodedText
    held_suffix: bytes = b""

    @property
    def text(self) -> str:
        return self.decoded.text


_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _codec_name(codec: str) -> str:
    return codecs.lookup(codec).name


def decode_content(data: bytes) -> DecodedText:
    """Decode complete file/content bytes using the repository detection order."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return DecodedText(
                data[len(bom) :].decode(codec),
                _codec_name(codec),
                bom,
                source=f"{codec} BOM",
            )

    for codec in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        return DecodedText(text, _codec_name(codec), source=f"strict {codec}")
    raise AssertionError("latin-1 decodes every byte sequence")


def detect_reader(
    reader: BinaryIO,
    chunk_size: int = 64 * 1024,
    codec: str | None = None,
) -> DecodedText:
    """Detect seekable file content without retaining the whole file in memory.

    The policy is identical to :func:`decode_content`. The returned text is empty;
    callers stream-decode with ``codec`` after skipping ``bom``.
    """
    start = reader.tell()
    try:
        if codec is not None:
            canonical = _codec_name(codec)
            decoder = codecs.getincrementaldecoder(canonical)(errors="strict")
            try:
                while chunk := reader.read(chunk_size):
                    decoder.decode(chunk, final=False)
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                return DecodedText(
                    "", canonical, had_errors=True, loss=True,
                    source="explicit codec; invalid input replaced",
                )
            return DecodedText("", canonical, source="explicit codec; strict decode")
        head = reader.read(3)
        for bom, codec in _BOMS:
            if head.startswith(bom):
                reader.seek(start + len(bom))
                decoder = codecs.getincrementaldecoder(codec)(errors="strict")
                while chunk := reader.read(chunk_size):
                    decoder.decode(chunk, final=False)
                decoder.decode(b"", final=True)
                return DecodedText(
                    "", _codec_name(codec), bom, source=f"{codec} BOM"
                )

        for codec in ("utf-8", "cp1252"):
            reader.seek(start)
            decoder = codecs.getincrementaldecoder(codec)(errors="strict")
            try:
                while chunk := reader.read(chunk_size):
                    decoder.decode(chunk, final=False)
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                continue
            return DecodedText("", _codec_name(codec), source=f"strict {codec}")
        return DecodedText("", _codec_name("latin-1"), source="strict latin-1")
    finally:
        reader.seek(start)


def encode_replacement(decoded: DecodedText, text: str) -> bytes:
    """Encode replacement text with the original codec and exact BOM bytes."""
    return decoded.bom + text.encode(decoded.codec)


def decode_explicit(data: bytes, codec: str) -> DecodedText:
    """Decode all bytes with an explicitly selected codec, replacing errors.

    Unlike content detection this never consumes a BOM or cursor fragments.
    """
    canonical = _codec_name(codec)
    try:
        text = data.decode(canonical, errors="strict")
    except UnicodeDecodeError:
        return DecodedText(
            data.decode(canonical, errors="replace"),
            canonical,
            had_errors=True,
            loss=True,
            source="explicit codec; invalid input replaced",
        )
    return DecodedText(text, canonical, source="explicit codec; strict decode")


def incomplete_codec_suffix(data: bytes, codec: str) -> bytes:
    """Return bytes buffered by an incremental decoder at the end of *data*.

    This works even when malformed bytes occur earlier in the range and covers
    multibyte Windows OEM codecs as well as UTF-8.
    """
    decoder = codecs.getincrementaldecoder(codec)(errors="replace")
    decoder.decode(data, final=False)
    pending, _ = decoder.getstate()
    return pending


def incomplete_utf8_suffix(data: bytes) -> bytes:
    """Compatibility wrapper for callers that specifically need UTF-8."""
    return incomplete_codec_suffix(data, "utf-8")


def decode_byte_range(data: bytes, codec: str) -> ByteRangeText:
    """Decode an arbitrary byte range without dropping legacy-codec bytes.

    Any multibyte codec may end in an incomplete character; that suffix remains
    unacknowledged until a later contiguous range completes it. Invalid leading
    bytes are decoded as replacement characters rather than silently skipped.
    """
    canonical = _codec_name(codec)
    suffix = b""
    retained = data
    suffix = incomplete_codec_suffix(retained, canonical)
    if suffix:
        retained = retained[: -len(suffix)]
    return ByteRangeText(decode_explicit(retained, canonical), held_suffix=suffix)


def decode_filename(name: bytes | str) -> str:
    """Decode an OS filename without applying file-content heuristics."""
    if isinstance(name, str):
        return name
    try:
        return os.fsdecode(name)
    except UnicodeDecodeError:
        # Windows' os.fsdecode uses surrogatepass, which can still reject
        # malformed byte records emitted by an external producer. Preserve the
        # bytes as filesystem surrogates instead of crashing the search result.
        return name.decode(sys.getfilesystemencoding(), errors="surrogateescape")
