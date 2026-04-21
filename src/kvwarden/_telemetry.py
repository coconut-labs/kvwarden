"""Opt-in anonymous install/usage telemetry.

Every failure path is silently swallowed -- telemetry never blocks,
delays, or breaks the CLI. ``KVWARDEN_TELEMETRY=0`` and an empty
``KVWARDEN_TELEMETRY_URL`` short-circuit before any file I/O. Full
privacy contract: ``docs/privacy/telemetry.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

EventName = Literal["install_first_run", "serve_started", "doctor_ran"]
_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"install_first_run", "serve_started", "doctor_ran"}
)
_ALLOWED_EVENT_KEYS: frozenset[str] = frozenset(
    {"install_id", "version", "python_version", "platform", "gpu_class", "event", "ts"}
)

# Build-time constant. Ship empty; distributors set via env var at package
# build time. An empty URL means "never send", even if the user opted in.
_DEFAULT_URL = ""


def _config_path() -> Path:
    """Return the telemetry config path (honors ``XDG_CONFIG_HOME``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "kvwarden" / "telemetry.json"


def _load_config() -> dict | None:
    """Read the persisted config, or ``None`` if missing/corrupt."""
    try:
        p = _config_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_config(enabled: bool, install_id: str) -> None:
    """Persist the user's choice. Silent on failure."""
    try:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(enabled), "install_id": install_id}))
    except Exception:
        pass


def _env_disabled() -> bool:
    """``KVWARDEN_TELEMETRY=0`` (or false/no/off) forces off."""
    return os.environ.get("KVWARDEN_TELEMETRY", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def _endpoint() -> str:
    """Resolve the telemetry URL; env beats compile-time default."""
    return os.environ.get("KVWARDEN_TELEMETRY_URL", _DEFAULT_URL).strip()


def _gpu_class() -> str:
    """Best-effort GPU class from nvidia-smi. Never raises.

    Returns ``h100`` | ``a100`` | ``rtx4090`` | ``other`` | ``none``.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return "none"
        name = line[0].lower()
        if "h100" in name:
            return "h100"
        if "a100" in name:
            return "a100"
        if "4090" in name:
            return "rtx4090"
        return "other"
    except Exception:
        return "none"


def _valid_install_id(v: object) -> bool:
    """Light sanity check on a persisted install_id.

    Not a privacy guarantee (the file is ours, the uuid is ours) -- just
    enough to catch a corrupt or hand-edited config without crashing.
    """
    return isinstance(v, str) and 8 <= len(v) <= 64


def _post_event_blocking(url: str, payload: dict) -> None:
    """POST the event. Runs on a daemon thread. Silent on any failure."""
    try:
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        target = url if url.endswith("/event") else url + "/event"
        req = urllib.request.Request(
            target,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"kvwarden/{payload.get('version', '?')}",
            },
            method="POST",
        )
        # urllib's `timeout` is per-socket-op; 5s is an acceptable upper
        # bound for a one-shot POST on a daemon thread.
        with urllib.request.urlopen(req, timeout=5):  # noqa: S310
            pass
    except Exception:
        pass


def _prompt_user() -> bool:
    """Show the one-paragraph prompt. Default No on empty / any failure."""
    try:
        sys.stderr.write(
            "\nKVWarden can send anonymous install stats (install ID, version, "
            "Python/OS, GPU class, and which command you ran) to help prioritize "
            "work. No prompts, model names, tenant IDs, or IPs are ever sent. "
            "Share? [y/N] "
        )
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in {"y", "yes"}
    except Exception:
        return False


def maybe_prompt_and_record_event(event: EventName) -> None:
    """Top-level entry point invoked from the CLI.

    Called once per CLI invocation. On first interactive run, prompts the
    user. On every run, if the user opted in and a URL is configured, fires
    off a daemon thread to POST the event. Silent on every failure.
    """
    try:
        if event not in _ALLOWED_EVENTS:
            return  # defensive: caller bug, don't send unknown events.

        if _env_disabled():
            return

        cfg = _load_config()
        if cfg is None:
            # First run. Prompt only if interactive; otherwise default No
            # and persist so we never prompt again (e.g. CI, docker).
            install_id = str(uuid.uuid4())
            enabled = (
                _prompt_user() if sys.stdin.isatty() and sys.stderr.isatty() else False
            )
            _save_config(enabled, install_id)
            cfg = {"enabled": enabled, "install_id": install_id}

        if not cfg.get("enabled"):
            return

        install_id = cfg.get("install_id")
        if not _valid_install_id(install_id):
            return  # corrupt file; safer to skip than to invent one.

        url = _endpoint()
        if not url:
            return  # compile-time disabled; no receiver to post to.

        try:
            version = _pkg_version("kvwarden")
        except PackageNotFoundError:
            version = "0.0.0+unknown"

        p = sys.platform
        plat = (
            "linux"
            if p.startswith("linux")
            else (
                "darwin"
                if p == "darwin"
                else ("win32" if p.startswith("win") else "other")
            )
        )

        payload = {
            "install_id": install_id,
            "version": version,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform": plat,
            "gpu_class": _gpu_class(),
            "event": event,
            "ts": int(time.time()),
        }
        threading.Thread(
            target=_post_event_blocking,
            args=(url, payload),
            daemon=True,
        ).start()
    except Exception:
        # Absolute backstop: telemetry must never break the CLI.
        pass


def set_enabled(value: bool) -> dict:
    """Flip the enabled flag; preserve (or mint) an install_id."""
    cfg = _load_config() or {}
    install_id = cfg.get("install_id")
    if not _valid_install_id(install_id):
        install_id = str(uuid.uuid4())
    _save_config(bool(value), install_id)
    return {"enabled": bool(value), "install_id": install_id}


def get_status() -> dict:
    """Return a dict describing the current telemetry state."""
    cfg = _load_config()
    return {
        "enabled": bool(cfg.get("enabled")) if cfg else False,
        "configured": cfg is not None,
        "install_id": (cfg or {}).get("install_id"),
        "env_disabled": _env_disabled(),
        "endpoint_set": bool(_endpoint()),
        "config_path": str(_config_path()),
    }
