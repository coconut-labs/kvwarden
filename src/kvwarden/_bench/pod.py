"""Optional RunPod A100 provisioning for ``reproduce-hero --pod``.

Zero-new-deps: ``runpod`` is lazy-imported. If it isn't installed, we
bail with a one-line hint rather than making it a hard dep.

Contract used by :mod:`kvwarden._bench.hero`:

* ``ensure_pod(console, delete_on_exit) -> PodContext``
* ``pod_signal_handler(ctx) -> signal handler``
* ``PodContext.base_url`` / ``PodContext.teardown()`` (idempotent)
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console

_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
_GPU_TYPE = "NVIDIA A100 80GB PCIe"
_BOOT_TIMEOUT_S = 900  # 15 min; cold A100 pods can queue behind demand


@dataclass
class PodContext:
    """Provisioned-pod book-keeping with an idempotent teardown."""

    pod_id: str
    base_url: str
    runpod_mod: Any
    delete_on_exit: bool
    console: Console
    _torn_down: bool = field(default=False, repr=False)

    def teardown(self) -> None:
        """Delete the pod unless --no-delete. Never raises from cleanup."""
        if self._torn_down or not self.delete_on_exit:
            self._torn_down = True
            return
        try:
            self.runpod_mod.terminate_pod(self.pod_id)
            self.console.print(f"[green]✓[/green] terminated pod [cyan]{self.pod_id}[/cyan]")
        except Exception as exc:
            self.console.print(
                f"[yellow]![/yellow] failed to terminate pod {self.pod_id}: {exc}\n"
                f"  Terminate manually at https://runpod.io/console/pods"
            )
        self._torn_down = True


def _bail(console: Console, msg: str) -> None:  # pragma: no cover
    console.print(f"[red]✗[/red] {msg}")
    raise SystemExit(2)


def _wait_for_proxy(mod: Any, pod_id: str, port: int) -> str:
    """Poll until the pod exposes a public URL on ``port``, or time out."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        info = mod.get_pod(pod_id) or {}
        runtime = info.get("runtime") or {}
        for p in runtime.get("ports") or []:
            if int(p.get("privatePort", -1)) == port and p.get("isIpPublic"):
                return f"http://{p.get('ip')}:{p.get('publicPort')}"
        # RunPod's managed HTTPS proxy is up once the container is running.
        if runtime.get("uptimeInSeconds", 0) > 30:
            return f"https://{pod_id}-{port}.proxy.runpod.net"
        time.sleep(10)
    raise RuntimeError(
        f"Pod {pod_id} did not expose port {port} within {_BOOT_TIMEOUT_S}s. "
        "Check https://runpod.io/console/pods for its status."
    )


def ensure_pod(
    *, console: Console, delete_on_exit: bool, port: int = 8000
) -> PodContext:
    """Provision a 1x A100 pod. SystemExit(2) with a hint on missing prereqs."""
    if not os.environ.get("RUNPOD_API_KEY"):
        _bail(console, "--pod requires RUNPOD_API_KEY (https://runpod.io/console/user/settings).")
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        _bail(console, "--pod requires HF_TOKEN for gated Llama-3.1-8B weights.")
    try:
        import runpod as _rp  # lazy; zero-new-deps
    except ImportError:
        _bail(console, "`pip install runpod` required for --pod (not a core kvwarden dep).")
        return None  # unreachable; appeases the type-checker

    _rp.api_key = os.environ["RUNPOD_API_KEY"]
    console.print("[bold]Provisioning 1x A100 pod...[/bold]")
    try:
        pod = _rp.create_pod(
            name="kvwarden-reproduce-hero",
            image_name=_IMAGE, gpu_type_id=_GPU_TYPE, cloud_type="SECURE",
            gpu_count=1, container_disk_in_gb=100, volume_in_gb=50,
            ports=f"{port}/http",
            env={"HF_TOKEN": hf_token, "KVWARDEN_AUTO_SERVE": "1"},
        )
    except Exception as exc:
        _bail(console, f"runpod.create_pod failed: {exc}")
        return None
    pod_id = str(pod.get("id") or pod.get("pod_id") or "").strip()
    if not pod_id:
        _bail(console, f"runpod returned no pod id (got {pod!r}).")
        return None
    console.print(f"  pod id [cyan]{pod_id}[/cyan] — waiting for port {port}...")
    try:
        url = _wait_for_proxy(_rp, pod_id, port)
    except Exception as exc:
        try:
            _rp.terminate_pod(pod_id)
        except Exception:
            pass
        _bail(console, str(exc))
        return None
    console.print(f"  base-url [cyan]{url}[/cyan]")
    return PodContext(
        pod_id=pod_id, base_url=url, runpod_mod=_rp,
        delete_on_exit=delete_on_exit, console=console,
    )


def pod_signal_handler(ctx: PodContext) -> Callable[[int, Any], None]:
    """Return a handler that tears down the pod on SIGINT/SIGTERM."""

    def _handler(signum: int, _frame: Any) -> None:
        ctx.console.print(f"\n[yellow]![/yellow] caught signal {signum}, terminating pod...")
        ctx.teardown()
        sys.exit(130)

    return _handler


__all__ = ["PodContext", "ensure_pod", "pod_signal_handler"]
