"""Unit tests for scholight.search.common.fusion — abstract length penalty."""

from __future__ import annotations

import math

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────


def _expected_weight(length: int, midpoint: int = 120, steepness: float = 10.0) -> float:
    """Reference implementation — must match _abstract_length_penalty exactly."""
    lg = math.log10(max(length, 1))
    lm = math.log10(midpoint)
    return 1.0 / (1.0 + math.exp(-steepness * (lg - lm)))


# ── _abstract_length_penalty ───────────────────────────────────────────────


class TestAbstractLengthPenalty:
    """Tests for the sigmoid-on-log-length penalty function.

    All tests import _abstract_length_penalty from scholight.search.common.fusion.
    This function does NOT exist yet — these tests are RED by design and will
    raise ImportError until the function is implemented.
    """

    # The import is done inside an _import helper so all tests fail with the
    # same ImportError.  Once the function exists, each test exercises distinct
    # behaviour.

    @staticmethod
    def _pen(length: int) -> float:
        """Lazy import — fails with ImportError until function is added."""
        from scholight.search.common.fusion import _abstract_length_penalty

        return _abstract_length_penalty(length)

    # ── edge cases ─────────────────────────────────────────────────────────

    def test_zero_length_treated_as_one(self) -> None:
        """length=0 is clamped to length=1 → weight ≈ 0.000 (extreme penalty)."""
        result = self._pen(0)
        expected = _expected_weight(0)  # log10(1) ≈ log10(0+)
        assert result == pytest.approx(expected, abs=1e-6)

    def test_negative_length_treated_as_one(self) -> None:
        """Negative lengths are also clamped via max(1) → same as length=1."""
        result = self._pen(-5)
        expected = _expected_weight(1)
        assert result == pytest.approx(expected, abs=1e-6)

    # ── very-short abstracts (near-zero penalty) ───────────────────────────

    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (10, _expected_weight(10)),
            (20, _expected_weight(20)),
            (30, _expected_weight(30)),
        ],
    )
    def test_very_short_heavily_penalized(self, length: int, expected: float) -> None:
        """Abstracts ≤ 30 chars receive weight near zero."""
        result = self._pen(length)
        assert result == pytest.approx(expected, abs=1e-4)

    # ── below midpoint (partial penalty, monotonically increasing) ─────────

    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (50, _expected_weight(50)),
            (70, _expected_weight(70)),
            (100, _expected_weight(100)),
        ],
    )
    def test_below_midpoint_partial_penalty(self, length: int, expected: float) -> None:
        """Abstracts < 120 chars receive partial penalty (0 < weight < 0.5)."""
        result = self._pen(length)
        assert 0.0 < result < 0.5
        assert result == pytest.approx(expected, rel=0.01)

    # ── midpoint (half weight) ─────────────────────────────────────────────

    def test_at_midpoint_half_weight(self) -> None:
        """At exactly 120 chars, weight = 0.5 (the sigmoid midpoint)."""
        result = self._pen(120)
        assert result == pytest.approx(0.5, abs=1e-6)

    # ── above midpoint (weight approaches 1) ───────────────────────────────

    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (150, _expected_weight(150)),
            (200, _expected_weight(200)),
            (300, _expected_weight(300)),
            (500, _expected_weight(500)),
            (1000, _expected_weight(1000)),
        ],
    )
    def test_above_midpoint_approaches_one(self, length: int, expected: float) -> None:
        """Abstracts > 120 chars receive progressively higher weight approaching 1."""
        result = self._pen(length)
        assert 0.5 < result < 1.0
        assert result == pytest.approx(expected, rel=0.01)

    # ── monotonicity ───────────────────────────────────────────────────────

    def test_monotonic_increasing(self) -> None:
        """weight(length) strictly increases with length."""
        lengths = [1, 5, 10, 20, 30, 50, 70, 100, 120, 150, 200, 300, 500, 1000, 5000]
        weights = [self._pen(ln) for ln in lengths]
        for i in range(len(weights) - 1):
            assert weights[i] < weights[i + 1], (
                f"weight({lengths[i]})={weights[i]} >= weight({lengths[i + 1]})={weights[i + 1]}"
            )

    # ── convergence ────────────────────────────────────────────────────────

    def test_upper_bound_is_one(self) -> None:
        """Weight is bounded above by 1.0 regardless of abstract length."""
        for length in (5000, 10000, 100000):
            result = self._pen(length)
            assert 0.999 < result <= 1.0, f"length={length}: {result=} not in (0.999, 1.0]"

    def test_very_long_converges_to_one(self) -> None:
        """At extreme lengths (1e6 chars), weight is essentially 1.0."""
        result = self._pen(1_000_000)
        assert result == pytest.approx(1.0, abs=1e-8)
