"""
Golden tests for the edit matcher. Run:  uv run pytest  (from edit-mcp/)

These are pure-stdlib (no fastmcp, no rg) and exercise the non-ASCII failure
modes that break naive str.replace — the reason this tool exists.
"""
import unicodedata as U

import pytest

from edit_mcp.matcher import (
    EditError,
    detect_line_ending,
    find_and_replace,
    normalize_for_fuzzy,
    strip_bom,
)


# --- exact path preserves everything --------------------------------------
def test_exact_match_is_byte_perfect():
    out, strat, n = find_and_replace("foo bar baz", "bar", "QUX")
    assert out == "foo QUX baz"
    assert strat == "exact" and n == 1


def test_exact_preferred_over_fuzzy_when_available():
    # straight quotes present exactly -> no normalization happens
    out, strat, _ = find_and_replace('x = "a"', '"a"', '"b"')
    assert strat == "exact" and out == 'x = "b"'


# --- the non-ASCII fixes (fuzzy fallback) ---------------------------------
def test_smart_double_quotes():
    out, strat, _ = find_and_replace('print(“hi”)', 'print("hi")', 'print("bye")')
    assert strat == "fuzzy" and out == 'print("bye")'


def test_smart_single_quotes():
    out, strat, _ = find_and_replace("x = ‘a’", "x = 'a'", "x = 'b'")
    assert strat == "fuzzy" and out == "x = 'b'"


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−"])
def test_all_dash_variants_fold_to_hyphen(dash):
    out, strat, _ = find_and_replace(f"a {dash} b", "a - b", "a - c")
    assert strat == "fuzzy" and out == "a - c"


def test_non_breaking_and_exotic_spaces():
    for sp in [" ", " ", " ", " ", "　"]:
        out, strat, _ = find_and_replace(f"a{sp}=1", "a =1", "a =2")
        assert strat == "fuzzy" and out == "a =2"


def test_nfd_on_disk_nfc_from_model():
    """The classic macOS bug: file stored decomposed, model emits composed."""
    disk = U.normalize("NFD", "café = 0")
    model_old = U.normalize("NFC", "café = 0")
    out, strat, _ = find_and_replace(disk, model_old, "café = 100")
    assert strat == "fuzzy" and "= 100" in out


def test_fullwidth_nfkc_folding():
    # full-width digits/letters normalize under NFKC
    out, strat, _ = find_and_replace("ｖａｌ = 1", "val = 1", "val = 2")
    assert strat == "fuzzy" and out == "val = 2"


def test_trailing_whitespace_difference():
    out, strat, _ = find_and_replace("x = 1   \ny = 2", "x = 1\ny = 2", "x = 1\ny = 9")
    assert strat == "fuzzy" and "y = 9" in out


# --- preservation guarantees ----------------------------------------------
def test_untouched_exotic_lines_preserved_verbatim():
    content = "a = “keep”\nb = “change”\nc = “keep”"
    out, _, _ = find_and_replace(content, 'b = "change"', 'b = "done"')
    assert "a = “keep”" in out          # exotic chars on other lines untouched
    assert "c = “keep”" in out
    assert 'b = "done"' in out


def test_crlf_preserved_on_write():
    src = "l1\r\nTARGET\r\nl3"
    out, _, _ = find_and_replace(src, "TARGET", "HIT")
    assert out == "l1\r\nHIT\r\nl3"


def test_bom_preserved():
    src = "﻿hello world"
    out, _, _ = find_and_replace(src, "world", "there")
    assert out == "﻿hello there"


# --- uniqueness / errors ---------------------------------------------------
def test_duplicate_without_replace_all_raises():
    with pytest.raises(EditError, match="not unique"):
        find_and_replace("x\nx\nx", "x", "y")


def test_replace_all_exact():
    out, strat, n = find_and_replace("x\nx\nx", "x", "y", replace_all=True)
    assert out == "y\ny\ny" and n == 3


def test_replace_all_fuzzy():
    content = "a — b\nc — d"           # two em-dashes
    out, strat, n = find_and_replace(content, "a - b", "a - B", replace_all=False)
    assert out == "a - B\nc — d"       # only the unique-enough first one edited
    out2, strat2, n2 = find_and_replace("x — y\nx — y", "x - y", "x - Z", replace_all=True)
    assert n2 == 2 and out2 == "x - Z\nx - Z"


def test_replace_all_fuzzy_never_reedits_inserted_text():
    out, strategy, count = find_and_replace(
        '“x” “x”', '"x"', '"x" again', replace_all=True,
    )
    assert strategy == "fuzzy" and count == 2
    assert out == '"x" again "x" again'


def test_replace_all_fuzzy_preserves_untouched_lines():
    content = "keep — curly\nreplace — me twice: replace — me\nkeep “quotes”"
    out, _, count = find_and_replace(
        content, "replace - me", "done", replace_all=True,
    )
    assert count == 2
    assert out == "keep — curly\ndone twice: done\nkeep “quotes”"


def test_fuzzy_match_ending_at_newline_does_not_normalize_next_line():
    out, strategy, count = find_and_replace(
        "change — this\nkeep “this” curly\n",
        "change - this\n",
        "changed\n",
    )
    assert strategy == "fuzzy" and count == 1
    assert out == "changed\nkeep “this” curly\n"


def test_empty_old_string_raises():
    with pytest.raises(EditError, match="empty"):
        find_and_replace("abc", "", "x")


def test_no_match_raises_with_hint():
    with pytest.raises(EditError, match="Closest match"):
        find_and_replace("def alpha():\n    return 1", "def beta():\n    return 1", "x")


def test_fuzzy_preserves_unrelated_same_line_bytes_and_trailing_spaces():
    source = 'keep “smart” and café; change — me; tail ’   '
    out, strategy, _ = find_and_replace(source, "change - me", "changed")
    assert strategy == "fuzzy"
    assert out == 'keep “smart” and café; changed; tail ’   '


def test_nfkc_expansion_maps_to_original_span_only():
    out, strategy, _ = find_and_replace("left ㍿ right — keep", "株式会社", "company")
    assert strategy == "fuzzy"
    assert out == "left company right — keep"


@pytest.mark.parametrize(
    ("source", "old", "expected"),
    [
        ("Ａ¼B", "1", "ＡXB"),
        ("㍿", "株式", "X"),
        ("Ａ¼Ｂ¼C", "1⁄4B1⁄4", "ＡXC"),
    ],
)
def test_partial_nfkc_expansions_consume_only_their_source_clusters(source, old, expected):
    out, strategy, count = find_and_replace(source, old, "X")
    assert (out, strategy, count) == (expected, "fuzzy", 1)


def test_nfd_cluster_partial_normalized_match_consumes_complete_cluster():
    source = "a cafe\N{COMBINING ACUTE ACCENT} z"
    out, _, _ = find_and_replace(source, "é", "E")
    assert out == "a cafE z"


@pytest.mark.parametrize(
    ("source", "old", "expected"),
    [
        ("가", "가", "X"),
        ("ｶﾞ", "ガ", "X"),
    ],
)
def test_cross_character_nfkc_composition_maps_to_complete_source(source, old, expected):
    out, strategy, count = find_and_replace(source, old, "X")
    assert (out, strategy, count) == (expected, "fuzzy", 1)


@pytest.mark.parametrize(
    "source",
    ["cafe\N{COMBINING ACUTE ACCENT}", "가", "ｶﾞ", "Ａ¼B", "a — b\nnext   "],
)
def test_provenance_normalization_matches_public_normalization(source):
    from edit_mcp.matcher import _normalize_with_provenance

    normalized, provenance = _normalize_with_provenance(source)
    assert normalized == normalize_for_fuzzy(source)
    assert len(normalized) == len(provenance)


def test_multiple_expansions_replace_all_uses_nonoverlapping_source_snapshot():
    out, strategy, count = find_and_replace("㍿/㍿/keep", "株式", "X", replace_all=True)
    assert (out, strategy, count) == ("X/X/keep", "fuzzy", 2)


def test_multiline_expansion_boundaries_do_not_absorb_neighbors():
    out, _, _ = find_and_replace("left Ａ¼\n㍿ right", "1⁄4\n株式", "X")
    assert out == "left ＡX right"


# --- primitives ------------------------------------------------------------
def test_strip_bom():
    assert strip_bom("﻿abc") == ("﻿", "abc")
    assert strip_bom("abc") == ("", "abc")


def test_detect_line_ending():
    assert detect_line_ending("a\r\nb") == "\r\n"
    assert detect_line_ending("a\nb") == "\n"
    assert detect_line_ending("abc") == "\n"


def test_normalize_idempotent_on_ascii():
    s = "plain ascii text = 1"
    assert normalize_for_fuzzy(s) == s


def test_exact_preserves_mixed_separators_and_reuses_consumed_breaks():
    content = "outside\r\none\ntwo\rthree\r\ntail\n"
    result, strategy, _ = find_and_replace(
        content, "one\ntwo\nthree", "ONE\nTWO\nTHREE\nEXTRA"
    )
    assert strategy == "exact"
    assert result == "outside\r\nONE\nTWO\rTHREE\r\nEXTRA\r\ntail\n"


def test_fuzzy_preserves_mixed_separators_within_and_outside_match():
    content = "head\r\nsmart “quote”\rnext\ntail\r\n"
    result, strategy, _ = find_and_replace(
        content, 'smart "quote"\nnext', "changed\nNEXT"
    )
    assert strategy == "fuzzy"
    assert result == "head\r\nchanged\rNEXT\ntail\r\n"
