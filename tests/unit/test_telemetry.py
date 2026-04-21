"""Unit tests for ``kvwarden._telemetry``.

These tests must never hit the network. ``urllib.request.urlopen`` is
mocked for every test that exercises the POST path. File I/O is redirected
to a temporary ``XDG_CONFIG_HOME`` via a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kvwarden import _telemetry


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a pristine XDG config home + env scrubbed."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("KVWARDEN_TELEMETRY", raising=False)
    monkeypatch.delenv("KVWARDEN_TELEMETRY_URL", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# _gpu_class
# ---------------------------------------------------------------------------


class TestGpuClass:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("NVIDIA H100 80GB HBM3", "h100"),
            ("NVIDIA A100-SXM4-80GB", "a100"),
            ("NVIDIA GeForce RTX 4090", "rtx4090"),
            ("Tesla V100-SXM2-16GB", "other"),
        ],
    )
    def test_mapping(self, name: str, expected: str) -> None:
        with patch("kvwarden._telemetry.subprocess.run") as run:
            run.return_value = MagicMock(stdout=name + "\n")
            assert _telemetry._gpu_class() == expected

    def test_no_nvidia_smi(self) -> None:
        with patch("kvwarden._telemetry.subprocess.run") as run:
            run.side_effect = FileNotFoundError
            assert _telemetry._gpu_class() == "none"

    def test_empty_output(self) -> None:
        with patch("kvwarden._telemetry.subprocess.run") as run:
            run.return_value = MagicMock(stdout="")
            assert _telemetry._gpu_class() == "none"

    def test_subprocess_timeout(self) -> None:
        with patch("kvwarden._telemetry.subprocess.run") as run:
            import subprocess

            run.side_effect = subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=2)
            assert _telemetry._gpu_class() == "none"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_load_missing_returns_none(self) -> None:
        assert _telemetry._load_config() is None

    def test_roundtrip(self) -> None:
        _telemetry._save_config(True, "11111111-1111-1111-1111-111111111111")
        cfg = _telemetry._load_config()
        assert cfg == {
            "enabled": True,
            "install_id": "11111111-1111-1111-1111-111111111111",
        }

    def test_corrupt_file_returns_none(self, _isolate_config: Path) -> None:
        p = _telemetry._config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        assert _telemetry._load_config() is None

    def test_non_dict_payload_returns_none(self, _isolate_config: Path) -> None:
        p = _telemetry._config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[1, 2, 3]")
        assert _telemetry._load_config() is None


# ---------------------------------------------------------------------------
# env var + endpoint handling
# ---------------------------------------------------------------------------


class TestEnvAndEndpoint:
    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "FALSE", "No"])
    def test_env_disables(self, v: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KVWARDEN_TELEMETRY", v)
        assert _telemetry._env_disabled() is True

    def test_env_unset_does_not_disable(self) -> None:
        assert _telemetry._env_disabled() is False

    def test_env_url_beats_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        assert _telemetry._endpoint() == "https://example.org"

    def test_default_url_is_empty(self) -> None:
        assert _telemetry._endpoint() == ""


# ---------------------------------------------------------------------------
# maybe_prompt_and_record_event
# ---------------------------------------------------------------------------


class TestMaybePromptAndRecord:
    def test_env_off_short_circuits_before_file_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KVWARDEN_TELEMETRY", "0")
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="install_first_run")
        post.assert_not_called()
        assert _telemetry._load_config() is None  # nothing written

    def test_unknown_event_noop(self) -> None:
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="nope")  # type: ignore[arg-type]
        post.assert_not_called()

    def test_non_interactive_first_run_writes_disabled_and_does_not_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # stdin.isatty returns False → never prompt, never post, persist off.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="install_first_run")
        post.assert_not_called()
        cfg = _telemetry._load_config()
        assert cfg is not None
        assert cfg["enabled"] is False
        assert _telemetry._valid_install_id(cfg["install_id"])

    def test_opted_out_user_does_not_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _telemetry._save_config(False, "22222222-2222-2222-2222-222222222222")
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="serve_started")
        post.assert_not_called()

    def test_opted_in_but_empty_url_does_not_post(self) -> None:
        _telemetry._save_config(True, "33333333-3333-3333-3333-333333333333")
        # URL env unset by fixture → endpoint is empty.
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="serve_started")
        post.assert_not_called()

    def test_opted_in_with_url_fires_daemon_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _telemetry._save_config(True, "44444444-4444-4444-4444-444444444444")
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        monkeypatch.setattr("kvwarden._telemetry._gpu_class", lambda: "a100")

        # Replace Thread with a MagicMock so we can inspect the call without
        # actually spawning a thread (and thus without hitting the network).
        with patch("kvwarden._telemetry.threading.Thread") as thread_cls:
            inst = MagicMock()
            thread_cls.return_value = inst
            _telemetry.maybe_prompt_and_record_event(event="serve_started")

        assert thread_cls.called
        kwargs = thread_cls.call_args.kwargs
        assert kwargs["daemon"] is True
        target = kwargs["target"]
        assert target is _telemetry._post_event_blocking
        url, payload = kwargs["args"]
        assert url == "https://example.org"
        assert set(payload.keys()) == _telemetry._ALLOWED_EVENT_KEYS
        assert payload["install_id"] == "44444444-4444-4444-4444-444444444444"
        assert payload["event"] == "serve_started"
        assert payload["gpu_class"] == "a100"
        assert payload["platform"] in {"linux", "darwin", "win32", "other"}
        assert isinstance(payload["ts"], int)
        inst.start.assert_called_once()

    @pytest.mark.parametrize("bad_id", ["", "x", 42, None, "a" * 200])
    def test_corrupt_install_id_skips_post(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_config: Path,
        bad_id: object,
    ) -> None:
        # Hand-write a config with a bad install_id (too short/long/non-str).
        p = _telemetry._config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": True, "install_id": bad_id}))
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        with patch("kvwarden._telemetry._post_event_blocking") as post:
            _telemetry.maybe_prompt_and_record_event(event="serve_started")
        post.assert_not_called()

    def test_prompt_accepts_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setattr("sys.stdin.readline", lambda: "y\n")
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        with patch("kvwarden._telemetry.threading.Thread") as thread_cls:
            thread_cls.return_value = MagicMock()
            _telemetry.maybe_prompt_and_record_event(event="install_first_run")
        cfg = _telemetry._load_config()
        assert cfg is not None and cfg["enabled"] is True
        assert thread_cls.called

    def test_prompt_default_is_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setattr("sys.stdin.readline", lambda: "\n")
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        with patch("kvwarden._telemetry.threading.Thread") as thread_cls:
            _telemetry.maybe_prompt_and_record_event(event="install_first_run")
        cfg = _telemetry._load_config()
        assert cfg is not None and cfg["enabled"] is False
        thread_cls.assert_not_called()

    def test_never_reprompts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _telemetry._save_config(False, "55555555-5555-5555-5555-555555555555")
        called = {"n": 0}

        def fake_readline() -> str:
            called["n"] += 1
            return "y\n"

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setattr("sys.stdin.readline", fake_readline)
        _telemetry.maybe_prompt_and_record_event(event="serve_started")
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# set_enabled / get_status
# ---------------------------------------------------------------------------


class TestSetEnabledAndStatus:
    def test_set_enabled_mints_install_id_if_missing(self) -> None:
        assert _telemetry._load_config() is None
        new = _telemetry.set_enabled(True)
        assert new["enabled"] is True
        assert _telemetry._valid_install_id(new["install_id"])

    def test_set_enabled_preserves_install_id(self) -> None:
        _telemetry._save_config(False, "66666666-6666-6666-6666-666666666666")
        new = _telemetry.set_enabled(True)
        assert new["install_id"] == "66666666-6666-6666-6666-666666666666"
        assert new["enabled"] is True

    def test_get_status_unconfigured(self) -> None:
        st = _telemetry.get_status()
        assert st["configured"] is False
        assert st["enabled"] is False
        assert st["install_id"] is None
        assert st["env_disabled"] is False
        assert st["endpoint_set"] is False

    def test_get_status_with_config_and_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _telemetry._save_config(True, "77777777-7777-7777-7777-777777777777")
        monkeypatch.setenv("KVWARDEN_TELEMETRY", "0")
        monkeypatch.setenv("KVWARDEN_TELEMETRY_URL", "https://example.org")
        st = _telemetry.get_status()
        assert st["configured"] is True
        assert st["enabled"] is True
        assert st["install_id"] == "77777777-7777-7777-7777-777777777777"
        assert st["env_disabled"] is True
        assert st["endpoint_set"] is True


# ---------------------------------------------------------------------------
# _post_event_blocking — mocked, verifies we never touch the real network
# ---------------------------------------------------------------------------


class TestPostEventBlocking:
    def test_posts_to_event_path(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = MagicMock()
            _telemetry._post_event_blocking(
                "https://example.org",
                {
                    "install_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                    "version": "0.1.2",
                    "python_version": "3.12",
                    "platform": "linux",
                    "gpu_class": "a100",
                    "event": "serve_started",
                    "ts": 1_700_000_000,
                },
            )
        assert urlopen.called
        req = urlopen.call_args.args[0]
        assert req.full_url.endswith("/event")
        assert req.method == "POST"
        body = json.loads(req.data.decode("utf-8"))
        assert set(body.keys()) == _telemetry._ALLOWED_EVENT_KEYS

    def test_preserves_existing_event_suffix(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = MagicMock()
            _telemetry._post_event_blocking(
                "https://example.org/event",
                {
                    "install_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                    "version": "0.1.2",
                    "python_version": "3.12",
                    "platform": "linux",
                    "gpu_class": "none",
                    "event": "doctor_ran",
                    "ts": 1_700_000_000,
                },
            )
        req = urlopen.call_args.args[0]
        # Must not double-suffix.
        assert req.full_url == "https://example.org/event"

    def test_network_error_swallowed(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("no dns")):
            # Must not raise.
            _telemetry._post_event_blocking(
                "https://example.org",
                {
                    "install_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                    "version": "0.1.2",
                    "python_version": "3.12",
                    "platform": "linux",
                    "gpu_class": "none",
                    "event": "serve_started",
                    "ts": 1_700_000_000,
                },
            )
