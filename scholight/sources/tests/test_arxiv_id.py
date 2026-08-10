"""Tests for canonicalize_arxiv_id — repair + validate arXiv IDs."""

from __future__ import annotations

import pytest

from scholight.sources.arxiv import arxiv_artifact_stem, canonicalize_arxiv_id


class TestCanonicalPassThrough:
    """Canonical IDs should be returned unchanged."""

    def test_new_format_4digit_suffix(self) -> None:
        assert canonicalize_arxiv_id("0704.0001") == "0704.0001"

    def test_new_format_5digit_suffix(self) -> None:
        assert canonicalize_arxiv_id("1501.01234") == "1501.01234"

    def test_new_format_2014_boundary(self) -> None:
        assert canonicalize_arxiv_id("1412.1234") == "1412.1234"

    def test_new_format_2015_boundary(self) -> None:
        assert canonicalize_arxiv_id("1501.01234") == "1501.01234"

    def test_old_subject_format(self) -> None:
        assert canonicalize_arxiv_id("astro-ph/9411001") == "astro-ph/9411001"

    def test_old_subject_with_hyphens(self) -> None:
        assert canonicalize_arxiv_id("math-ph/0210025") == "math-ph/0210025"


class TestRepair:
    """Short IDs should be padded correctly."""

    def test_prefix_padding(self) -> None:
        assert canonicalize_arxiv_id("801.0001") == "0801.0001"

    def test_prefix_padding_3_digit_year(self) -> None:
        assert canonicalize_arxiv_id("912.5028") == "0912.5028"

    def test_suffix_padding_2007_era(self) -> None:
        assert canonicalize_arxiv_id("1002.49") == "1002.4900"

    def test_suffix_padding_3_digit(self) -> None:
        assert canonicalize_arxiv_id("1003.271") == "1003.2710"

    def test_both_short(self) -> None:
        assert canonicalize_arxiv_id("802.1") == "0802.1000"

    def test_2015_suffix_padding_to_5_digits(self) -> None:
        assert canonicalize_arxiv_id("1501.0008") == "1501.00080"

    def test_2016_5_digit_target(self) -> None:
        assert canonicalize_arxiv_id("1601.0015") == "1601.00150"

    def test_2019_5_digit_target(self) -> None:
        assert canonicalize_arxiv_id("1901.0001") == "1901.00010"

    def test_strips_whitespace(self) -> None:
        assert canonicalize_arxiv_id("  1501.01234  ") == "1501.01234"


class TestReject:
    """Non-parseable IDs should be rejected."""

    def test_garbage(self) -> None:
        assert canonicalize_arxiv_id("garbage") is None

    def test_empty(self) -> None:
        assert canonicalize_arxiv_id("") is None

    def test_missing_dot(self) -> None:
        assert canonicalize_arxiv_id("07040001") is None

    def test_old_subject_bad_format(self) -> None:
        assert canonicalize_arxiv_id("bad/1234") is None

    def test_underscore_not_slash(self) -> None:
        assert canonicalize_arxiv_id("astro-ph_9608163") is None

    def test_too_many_parts(self) -> None:
        assert canonicalize_arxiv_id("0704.0001.extra") is None


class TestEdgeCases:
    """Boundary cases."""

    def test_2007_january_canonical(self) -> None:
        assert canonicalize_arxiv_id("0701.0001") == "0701.0001"

    def test_2026_december_canonical(self) -> None:
        assert canonicalize_arxiv_id("2612.99999") == "2612.99999"

    def test_none_input(self) -> None:
        # If callers accidentally pass None, should not crash
        with pytest.raises(AttributeError):
            canonicalize_arxiv_id(None)  # type: ignore[arg-type]


class TestArtifactStem:
    """Semantic arXiv IDs must map to one safe artifact filename."""

    def test_modern_id_is_unchanged(self) -> None:
        assert arxiv_artifact_stem("2501.12345") == "2501.12345"

    def test_legacy_id_replaces_the_subject_separator(self) -> None:
        assert arxiv_artifact_stem("cs/0012009") == "cs-0012009"

    @pytest.mark.parametrize(
        "unsafe_id",
        ("../2501.12345", "/2501.12345", "cs\\0012009", "bad/1234"),
    )
    def test_unsafe_or_noncanonical_id_is_rejected(self, unsafe_id: str) -> None:
        assert arxiv_artifact_stem(unsafe_id) is None
