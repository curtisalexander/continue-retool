from __future__ import annotations

import codecs

import pytest

from continue_mcp_common.text import (
    decode_byte_range,
    decode_content,
    decode_explicit,
    detect_reader,
    encode_replacement,
)


@pytest.mark.parametrize("bom", [b"", codecs.BOM_UTF8])
def test_utf8_and_bom(bom: bytes) -> None:
    result = decode_content(bom + "café".encode())
    assert result.text == "café"
    assert result.codec == "utf-8"
    assert result.bom == bom
    assert not result.had_errors


@pytest.mark.parametrize(
    ("bom", "codec"),
    [(codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")],
)
def test_utf16_bom_round_trip(bom: bytes, codec: str) -> None:
    result = decode_content(bom + "old café".encode(codec))
    assert result.text == "old café"
    assert result.bom == bom
    assert encode_replacement(result, "new café") == bom + "new café".encode(codec)


def test_cp1252_and_latin1_detection() -> None:
    cp = decode_content("“café”".encode("cp1252"))
    assert (cp.text, cp.codec) == ("“café”", "cp1252")

    latin = decode_content(b"a\x81b")  # 0x81 is undefined in cp1252
    assert (latin.text, latin.codec) == ("a\x81b", "iso8859-1")


def test_malformed_explicit_utf8_reports_replacement_without_trimming() -> None:
    result = decode_explicit(b"a\xffb\xe2", "utf8")
    assert result.text == "a\ufffdb\ufffd"
    assert result.codec == "utf-8"
    assert result.had_errors and result.loss
    assert "replaced" in result.source


def test_utf8_byte_range_reports_split_fragments() -> None:
    result = decode_byte_range(b"\xa9middle\xe2\x82", "utf-8")
    assert result.text == "\ufffdmiddle"
    assert result.held_suffix == b"\xe2\x82"
    assert result.decoded.had_errors


def test_malformed_utf8_before_split_suffix_does_not_acknowledge_suffix() -> None:
    result = decode_byte_range(b"\xff\xe2", "utf-8")
    assert result.text == "\ufffd"
    assert result.held_suffix == b"\xe2"


def test_dbcs_byte_range_holds_incomplete_character() -> None:
    raw = "あ".encode("cp932")
    first = decode_byte_range(raw[:1], "cp932")
    assert first.text == "" and first.held_suffix == raw[:1]
    assert decode_byte_range(raw, "cp932").text == "あ"


@pytest.mark.parametrize(
    ("raw", "expected"), [(b"caf\xe9", "café"), (b"\x93hi\x94", "“hi”")]
)
def test_cp1252_ranges_never_drop_legacy_bytes(raw: bytes, expected: str) -> None:
    result = decode_byte_range(raw, "cp1252")
    assert result.text == expected
    assert result.held_suffix == b""
    assert encode_replacement(result.decoded, result.text) == raw


def test_stream_detection_matches_complete_content_without_retaining_text(tmp_path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes(("line\n" * 100_000 + "café — end\n").encode("cp1252"))
    with path.open("rb") as reader:
        detected = detect_reader(reader, chunk_size=127)
        assert reader.tell() == 0
    complete = decode_content(path.read_bytes())
    assert detected.text == ""
    assert (detected.codec, detected.bom) == (complete.codec, complete.bom)
