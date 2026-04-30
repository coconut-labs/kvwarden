"""Unit tests for M4 harness flags --prefix-overlap (#120) + --bias-flooder-cost (#122).

Both features wrap or instrument the existing PromptSampler / Poisson loop
without changing pre-Gate-2.2 behavior when their flags are at the default
(prefix-overlap=0.0 / bias-flooder-cost=1.0). These tests pin the new
contracts and the no-flag backward-compat invariant.
"""

from __future__ import annotations

import logging
import random
import sys
from collections import deque
from pathlib import Path

import pytest

# Add the benchmark script dir so we can import the module under test.
_BENCH_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "benchmarks" / "scripts"
)
sys.path.insert(0, str(_BENCH_SCRIPTS))

from benchmark_n_tenant_single_model import (  # noqa: E402
    PROMPTS,
    build_shared_prefix,
    make_legacy_sampler,
    make_prefix_overlap_sampler,
    should_apply_bias,
)

# ---------------------------------------------------------------------------
# Issue #120: --prefix-overlap
# ---------------------------------------------------------------------------


class TestPrefixOverlapWrapper:
    def test_overlap_zero_yields_no_prefix(self) -> None:
        """overlap_fraction=0.0 (the default) must pass through unchanged.

        Equivalent to the pre-flag baseline: every draw equals the inner
        sampler's draw byte-for-byte.
        """
        prefix = "SHARED_PREFIX_TOKEN_BLOB"
        base = make_legacy_sampler()
        wrapped = make_prefix_overlap_sampler(
            inner=base, shared_prefix=prefix, overlap_fraction=0.0, seed=42
        )
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        for _ in range(50):
            via_wrap, len_w = wrapped(rng_a)
            via_direct = rng_b.choice(PROMPTS)
            assert via_wrap == via_direct
            assert len_w == 0
            assert prefix not in via_wrap

    def test_overlap_one_yields_prefix_on_every_call(self) -> None:
        """overlap_fraction=1.0 must prepend the prefix on every draw."""
        prefix = "DETERMINISTIC_SHARED_PREFIX"
        base = make_legacy_sampler()
        wrapped = make_prefix_overlap_sampler(
            inner=base, shared_prefix=prefix, overlap_fraction=1.0, seed=42
        )
        rng = random.Random(7)
        for _ in range(50):
            prompt, length = wrapped(rng)
            assert prompt.startswith(prefix + " ")
            # Length contract: original length is preserved (we do not
            # re-count prefix tokens).
            assert length == 0

    def test_overlap_half_matches_target_fraction(self) -> None:
        """overlap_fraction=0.5 over 1000 samples should give ~50% prefixed.

        Tolerance ±0.05 — well outside 3-sigma for Bernoulli(0.5, n=1000)
        which is ~0.047. With seed=42 the result is fully deterministic.
        """
        prefix = "PFX"
        base = make_legacy_sampler()
        wrapped = make_prefix_overlap_sampler(
            inner=base, shared_prefix=prefix, overlap_fraction=0.5, seed=42
        )
        rng = random.Random(0)
        n = 1000
        prefixed = sum(1 for _ in range(n) if wrapped(rng)[0].startswith(prefix + " "))
        frac = prefixed / n
        assert 0.45 <= frac <= 0.55, (
            f"prefixed fraction {frac:.3f} outside 0.5±0.05 over n={n} (seed=42)"
        )

    def test_seed_reproducibility(self) -> None:
        """Same seed → same prefix decisions across two independent constructions."""
        prefix = "PFX"
        base_a = make_legacy_sampler()
        base_b = make_legacy_sampler()
        wrap_a = make_prefix_overlap_sampler(
            inner=base_a, shared_prefix=prefix, overlap_fraction=0.5, seed=99
        )
        wrap_b = make_prefix_overlap_sampler(
            inner=base_b, shared_prefix=prefix, overlap_fraction=0.5, seed=99
        )
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        decisions_a = [wrap_a(rng_a)[0].startswith(prefix + " ") for _ in range(100)]
        decisions_b = [wrap_b(rng_b)[0].startswith(prefix + " ") for _ in range(100)]
        assert decisions_a == decisions_b

    def test_decisions_independent_of_arrival_rng_use(self) -> None:
        """Prefix decisions use a separate RNG so they are stable even if the
        inner sampler's rng usage shifts. Pulling extra entries from the
        arrival rng before each sample must not change which draws are
        prefixed.
        """
        prefix = "PFX"

        def base_one(rng: random.Random) -> tuple[str, int]:
            return rng.choice(PROMPTS), 0

        def base_two(rng: random.Random) -> tuple[str, int]:
            # Burns one extra rng draw per call.
            _ = rng.random()
            return rng.choice(PROMPTS), 0

        wrap_one = make_prefix_overlap_sampler(
            inner=base_one, shared_prefix=prefix, overlap_fraction=0.5, seed=12
        )
        wrap_two = make_prefix_overlap_sampler(
            inner=base_two, shared_prefix=prefix, overlap_fraction=0.5, seed=12
        )
        rng_one = random.Random(33)
        rng_two = random.Random(33)
        dec_one = [wrap_one(rng_one)[0].startswith(prefix + " ") for _ in range(80)]
        dec_two = [wrap_two(rng_two)[0].startswith(prefix + " ") for _ in range(80)]
        assert dec_one == dec_two

    def test_shared_prefix_builder_seed_stable(self) -> None:
        """build_shared_prefix is reproducible from --seed."""
        a = build_shared_prefix(shared_prefix_tokens=64, seed=42)
        b = build_shared_prefix(shared_prefix_tokens=64, seed=42)
        assert a == b
        assert isinstance(a, str) and len(a) > 0


# ---------------------------------------------------------------------------
# Issue #122: --bias-flooder-cost
# ---------------------------------------------------------------------------


class TestShouldApplyBias:
    def test_empty_deque_returns_false(self) -> None:
        dq: deque[float] = deque()
        assert should_apply_bias(dq, now=10.0, threshold=10, window_s=30.0) is False

    def test_below_threshold_returns_false(self) -> None:
        # 5 timestamps within window, threshold is 10 → False.
        dq: deque[float] = deque([float(i) for i in range(5)])
        assert should_apply_bias(dq, now=10.0, threshold=10, window_s=30.0) is False
        # No timestamps were stale, deque preserved.
        assert len(dq) == 5

    def test_above_threshold_returns_true(self) -> None:
        # 15 timestamps within window, threshold is 10 → True.
        dq: deque[float] = deque([float(i) for i in range(15)])
        assert should_apply_bias(dq, now=20.0, threshold=10, window_s=30.0) is True

    def test_stale_timestamps_dropped_from_window(self) -> None:
        """Timestamps older than now - window_s must be popped from the left.

        Setup: 8 stale + 5 fresh, threshold=10, window covers only the
        fresh ones. Expected: stale dropped, len=5, returns False.
        """
        stale = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        fresh = [100.0, 101.0, 102.0, 103.0, 104.0]
        dq: deque[float] = deque(stale + fresh)
        result = should_apply_bias(dq, now=105.0, threshold=10, window_s=10.0)
        assert result is False
        assert list(dq) == fresh


class TestBiasStateTransitionLogging:
    """The bench's per-tenant loop logs once per bias-state transition.

    These tests pin the log behavior of the helper without spinning up
    the async loop; we replay the logic the loop executes.
    """

    def _run_transitions(
        self,
        events: list[tuple[float, int, float]],
        threshold: int,
        window_s: float,
        multiplier: float,
        tenant_id: str,
        caplog_logger: logging.Logger,
    ) -> list[tuple[bool, bool]]:
        """Replay the loop's enter/exit-bias-state logic.

        events: list of (now, deque_target_len, _ignored). We synthesize a
        deque containing ``deque_target_len`` recent timestamps so
        should_apply_bias returns the desired truth value.
        """
        in_bias = False
        transitions: list[tuple[bool, bool]] = []
        bias_enabled = multiplier > 1.0 and threshold > 0
        for now, target_len, _ in events:
            if not bias_enabled:
                transitions.append((False, in_bias))
                continue
            # Synthesize a fresh deque of ``target_len`` timestamps inside
            # the window so should_apply_bias returns the value we want.
            dq: deque[float] = deque([now - 0.001] * target_len)
            apply = should_apply_bias(dq, now, threshold, window_s)
            if apply and not in_bias:
                caplog_logger.info(
                    "tenant=%s entering bias state (>%d reqs in %.1fs window, "
                    "multiplier=%.2fx)",
                    tenant_id,
                    threshold,
                    window_s,
                    multiplier,
                )
                in_bias = True
            elif not apply and in_bias:
                caplog_logger.info(
                    "tenant=%s exiting bias state (<=%d reqs in %.1fs window)",
                    tenant_id,
                    threshold,
                    window_s,
                )
                in_bias = False
            transitions.append((apply, in_bias))
        return transitions

    def test_logs_once_on_enter_and_once_on_exit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Use the harness-module logger so the test exercises the same
        # logger the production loop calls.
        from benchmark_n_tenant_single_model import logger as bench_logger

        # Sequence: below → above (enter) → above (no log) → below (exit) → below (no log)
        events = [
            (1.0, 5, 0.0),  # below threshold
            (2.0, 15, 0.0),  # crosses up → ENTER
            (3.0, 16, 0.0),  # still above → no log
            (4.0, 5, 0.0),  # crosses down → EXIT
            (5.0, 4, 0.0),  # still below → no log
        ]
        with caplog.at_level(logging.INFO, logger=bench_logger.name):
            self._run_transitions(
                events,
                threshold=10,
                window_s=30.0,
                multiplier=4.0,
                tenant_id="flooder",
                caplog_logger=bench_logger,
            )
        enter_msgs = [
            r for r in caplog.records if "entering bias state" in r.getMessage()
        ]
        exit_msgs = [
            r for r in caplog.records if "exiting bias state" in r.getMessage()
        ]
        assert len(enter_msgs) == 1, (
            f"expected exactly one enter-bias log, got {len(enter_msgs)}"
        )
        assert len(exit_msgs) == 1, (
            f"expected exactly one exit-bias log, got {len(exit_msgs)}"
        )
        # Verify the log message names the tenant + threshold.
        assert "flooder" in enter_msgs[0].getMessage()
        assert ">10 reqs" in enter_msgs[0].getMessage()

    def test_disabled_when_multiplier_is_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """multiplier=1.0 (the default) must not emit any bias-state logs."""
        from benchmark_n_tenant_single_model import logger as bench_logger

        events = [
            (1.0, 100, 0.0),  # would be above but bias is disabled
            (2.0, 50, 0.0),
        ]
        with caplog.at_level(logging.INFO, logger=bench_logger.name):
            self._run_transitions(
                events,
                threshold=10,
                window_s=30.0,
                multiplier=1.0,
                tenant_id="flooder",
                caplog_logger=bench_logger,
            )
        bias_msgs = [r for r in caplog.records if "bias state" in r.getMessage()]
        assert bias_msgs == []
