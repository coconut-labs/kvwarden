"""Unit tests for ``kvwarden bench reproduce-hero`` (network mocked)."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from kvwarden._bench import hero


class _FakeSession:
    """Async aiohttp.ClientSession stand-in. Both context-mgr and .get() work."""

    def __init__(self, health_status: int, models_payload: dict[str, Any]) -> None:
        self._h, self._m = health_status, models_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def get(self, url: str) -> "_FakeResp":
        return (_FakeResp(self._h, {}) if url.endswith("/health")
                else _FakeResp(200, self._m))


class _FakeResp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status, self._p = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._p

    async def text(self) -> str:
        return ""


# ── flavor + reference ──────────────────────────────────────────────────

def test_flavors_match_references() -> None:
    for key in ("2tenant", "n6", "n8"):
        assert hero.FLAVORS[key].num_quiet == hero.REFERENCES[key].num_quiet


def test_default_flavor_is_hero_2tenant() -> None:
    spec = hero.FLAVORS["2tenant"]
    assert (spec.num_quiet, spec.flooder_rps, spec.quiet_rps, spec.default_duration_s) == (
        1, 32.0, 1.0, 300.0
    )


def test_config_hints_exist() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for spec in hero.FLAVORS.values():
        assert (repo_root / spec.config_hint).exists(), spec.config_hint


# ── comparison math + rendering ─────────────────────────────────────────

def test_delta_badge_color_thresholds() -> None:
    assert "green" in hero._delta_badge(68.0, 61.5)      # ~10%
    assert "yellow" in hero._delta_badge(85.0, 61.5)     # ~38%
    assert "red" in hero._delta_badge(150.0, 61.5)       # ~144%
    assert hero._delta_badge(10.0, 0.0) == "[dim]n/a[/dim]"


def test_render_comparison_runs() -> None:
    hero.render_comparison("2tenant", 62.1, 0.93, None, Console())
    hero.render_comparison("n6", 62.1, 0.93, None, Console())


# ── preflight errors ────────────────────────────────────────────────────

async def test_connection_refused_hint() -> None:
    import aiohttp

    with patch("kvwarden._bench.hero.aiohttp.ClientSession") as mock_cls:
        mock_cls.return_value.__aenter__ = MagicMock(
            side_effect=aiohttp.ClientConnectorError(
                connection_key=MagicMock(), os_error=OSError(111, "refused")
            )
        )
        ok, hint = await hero._preflight_server(
            "http://localhost:8000", hero._HERO_MODEL, Console()
        )
    assert ok is False
    assert hint and "Could not connect" in hint and "kvwarden serve" in hint


async def test_wrong_model_hint() -> None:
    sess = _FakeSession(200, {"data": [{"id": "Qwen/Qwen2.5-7B"}]})
    with patch("kvwarden._bench.hero.aiohttp.ClientSession", return_value=sess):
        ok, hint = await hero._preflight_server(
            "http://localhost:8000", hero._HERO_MODEL, Console()
        )
    assert ok is False
    assert hint and "Llama-3.1-8B-Instruct" in hint and "/v1/models" in hint


async def test_health_non_200_hint() -> None:
    with patch("kvwarden._bench.hero.aiohttp.ClientSession",
               return_value=_FakeSession(503, {})):
        ok, hint = await hero._preflight_server(
            "http://localhost:8000", hero._HERO_MODEL, Console()
        )
    assert ok is False
    assert hint and "/health" in hint and "503" in hint


# ── result extraction ───────────────────────────────────────────────────

def test_count_429s_missing_file(tmp_path: Path) -> None:
    assert hero._count_429s(tmp_path / "missing.csv") == (0, 0)


def test_count_429s_parses_errors(tmp_path: Path) -> None:
    path = tmp_path / "tenant_flooder.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["tenant", "ttft_ms", "error"])
        w.writeheader()
        w.writerow({"tenant": "flooder", "ttft_ms": 20, "error": ""})
        w.writerow({"tenant": "flooder", "ttft_ms": 0, "error": "HTTP 429: rl"})
        w.writerow({"tenant": "flooder", "ttft_ms": 0, "error": "HTTP 429: rl"})
    assert hero._count_429s(path) == (2, 3)


def test_flooder_rate_empty_dir_is_zero(tmp_path: Path) -> None:
    assert hero._flooder_rate(tmp_path) == 0.0


def test_load_summary_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no summary.json"):
        hero._load_summary(tmp_path)


def test_build_report_schema() -> None:
    summary = {
        "flooder": {"ttft_p99_ms": 1700.0, "count_ok": 9000},
        "quiet_aggregate": {"ttft_p99_ms": 62.5, "count_ok": 311},
    }
    r = hero._build_report(
        hero.FLAVORS["2tenant"], summary, 0.93,
        "http://localhost:8000", 300.0, "NVIDIA A100-SXM4-80GB",
        "2026-04-21T15:00:00+00:00", "2026-04-21T15:05:00+00:00",
    )
    assert r["schema_version"] == 1 and r["flavor"] == "2tenant"
    assert r["user_result"]["quiet_aggregate_p99_ms"] == 62.5
    assert r["reference"]["tokenbucket_p99_ms"] == 61.5
    json.dumps(r)  # must serialize


# ── url parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, expected", [
    ("http://localhost:8000", ("localhost", 8000)),
    ("https://example.com:8080/", ("example.com", 8080)),
    ("http://example.com", ("example.com", 8000)),
])
def test_split_host_port(url: str, expected: tuple[str, int]) -> None:
    assert hero._split_host_port(url) == expected


# ── dispatch errors ─────────────────────────────────────────────────────

def test_unknown_flavor_exits_2() -> None:
    ns = argparse.Namespace(flavor="nope", duration_s=None,
                            base_url="http://localhost:8000",
                            pod=False, no_delete=False)
    with pytest.raises(SystemExit) as ei:
        hero.run_reproduce_hero(ns)
    assert ei.value.code == 2


def test_server_unreachable_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hero, "_port_listening", lambda h, p: False)
    ns = argparse.Namespace(flavor="2tenant", duration_s=5.0,
                            base_url="http://localhost:8000",
                            pod=False, no_delete=False)
    with pytest.raises(SystemExit) as ei:
        hero.run_reproduce_hero(ns)
    assert ei.value.code == 2


# ── pod teardown & signal handler ───────────────────────────────────────

def _mk_pod_ctx(delete: bool = True):
    from kvwarden._bench import pod as pod_mod

    return pod_mod.PodContext(
        pod_id="abc", base_url="http://ignored",
        runpod_mod=MagicMock(), delete_on_exit=delete, console=Console(),
    )


def test_signal_handler_terminates_and_exits_130() -> None:
    from kvwarden._bench import pod as pod_mod

    ctx = _mk_pod_ctx()
    with pytest.raises(SystemExit) as ei:
        pod_mod.pod_signal_handler(ctx)(2, None)
    assert ei.value.code == 130
    ctx.runpod_mod.terminate_pod.assert_called_once_with("abc")


def test_teardown_idempotent_and_no_delete() -> None:
    ctx = _mk_pod_ctx()
    ctx.teardown()
    ctx.teardown()
    assert ctx.runpod_mod.terminate_pod.call_count == 1

    no_del = _mk_pod_ctx(delete=False)
    no_del.teardown()
    assert no_del.runpod_mod.terminate_pod.call_count == 0
