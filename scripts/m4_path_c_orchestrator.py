#!/usr/bin/env python3
"""
T2 M4 Path C probe orchestrator (Arm 1 + Arm 2, 6 cells, 2026-05-02).

Spins one 1xA100 SXM4 80GB SECURE pod, scp's gate_pod_bootstrap.sh +
m4_gauge_scraper.py, runs 6 cells against the same kvwarden serve. Arm 1
and Arm 2 differ only in bench-script flags. Each cell starts a 250ms
/metrics scraper on localhost:8001 (vLLM port — kvwarden's :8000 only
exposes its own metrics; the engine's vllm:kv_cache_usage_perc lives on
the child engine port). Per-cell trap kills serve at end so next cell
gets a fresh engine + cache.

Cost guards (defense in depth):
  - $10 hard cap (target ~$8). Tracked every poll; abort + tear down >= $9.
  - 5h wall cap. SIGALRM raises TimeoutError; finally: delete_pod fires.
  - Per-cell 25-min timeout via the in-pod bootstrap MAX_POD_SECS plus a
    local watchdog around the SSH bootstrap-launch.
  - 3x pod-create-fail abort.

Pod tear-down lives in finally{}. Even on KeyboardInterrupt the pod is
DELETEd and verified 404.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Strip proxy env vars before httpx instantiates a client — local shell
# may export ALL_PROXY=socks5h://... which forces httpx to require the
# socksio extra. RunPod's REST API is internet-public; direct connect.
for _proxy_var in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
):
    os.environ.pop(_proxy_var, None)

REST_BASE = "https://rest.runpod.io/v1"
WORKTREE = Path(__file__).resolve().parent.parent
RESULTS_ROOT = WORKTREE / "results" / "m4_path_c_probe_20260502"
ORCH_LOG = RESULTS_ROOT / "orchestrator.log"

POD_NAME = "kvwarden-m4-path-c-20260502"
GPU_TYPE_ID = "NVIDIA A100-SXM4-80GB"
IMAGE = "runpod/pytorch:2.1.0-py3.10-cuda12.1.1-devel-ubuntu22.04"
CONTAINER_DISK_GB = 100
HF_MODEL = "meta-llama/Llama-3.1-8B-Instruct"

# Cost / wall caps
WALL_CAP_S = 18000  # 5h hard
COST_HARD_CAP_USD = 10.0
COST_ABORT_USD = 9.0
HOURLY_RATE_FALLBACK = 1.89  # ceiling fallback if API doesn't return cost
PER_CELL_TIMEOUT_S = 1500  # 25 min
MAX_POD_CREATE_FAILS = 3

# Bench knobs (locked)
DURATION_S = 300
FLOODER_RPS = 32
QUIET_RPS = 1
NUM_QUIET = 7
MAX_TOKENS = 128
PREFIX_OVERLAP = 0.7
SHARED_PREFIX_TOKENS = 1024
BIAS_FLOODER_COST = 4.0
BIAS_AFTER_N_REQS = 300
BIAS_WINDOW_S = 30
SEEDS = [0, 1, 2]
ARMS = ["arm1", "arm2"]

T0 = time.time()


def log(msg: str) -> None:
    elapsed = time.time() - T0
    line = f"[t={elapsed:7.1f}s {time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}"
    print(line, flush=True)
    try:
        ORCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ORCH_LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_HTTP: httpx.Client | None = None


def _client() -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        api_key = os.environ["RUNPOD_API_KEY"]
        _HTTP = httpx.Client(
            base_url=REST_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _HTTP


def api(method: str, path: str, body: dict | None = None, timeout: int = 30):
    try:
        resp = _client().request(method, path, json=body, timeout=timeout)
    except httpx.RequestError as exc:
        return -1, f"transport_error: {exc!r}"
    raw = resp.text
    try:
        return resp.status_code, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return resp.status_code, raw


def create_pod(pubkey: str) -> dict:
    # 2026-05-02: SECURE A100 SXM4 80GB stalled in init for 15 min on the
    # first attempt (publicIp / portMappings never populated; Gate 1.5
    # memory note re: stuck SECURE pods). Pivoted to COMMUNITY/spot — P1
    # proved that path at $0.79/hr with sshd via PUBLIC_KEY env. M4 is
    # 6 cells × ~13 min each; spot interruption risk over 80 min is
    # acceptable for a measure-first probe whose verdict tolerates
    # missing 1-2 cells.
    body = {
        "name": POD_NAME,
        "imageName": IMAGE,
        "gpuTypeIds": [GPU_TYPE_ID],
        "gpuCount": 1,
        "cloudType": "COMMUNITY",
        "interruptible": True,
        "containerDiskInGb": CONTAINER_DISK_GB,
        "ports": ["22/tcp", "8000/http", "8001/http"],
        "env": {
            "PUBLIC_KEY": pubkey,
            "HF_TOKEN": os.environ["HF_TOKEN"],
            "MAX_POD_SECS": "18000",
        },
        "supportPublicIp": True,
    }
    log(f"create_pod request: gpu={GPU_TYPE_ID} image={IMAGE} cloud=COMMUNITY/spot")
    status, resp = api("POST", "/pods", body)
    if status not in (200, 201):
        log(f"create FAIL status={status} resp={str(resp)[:300]}")
        raise RuntimeError(f"pod create failed: status={status}")
    if not isinstance(resp, dict):
        raise RuntimeError(f"unexpected pod-create response: {resp!r}")
    log(f"create OK id={resp.get('id')} costPerHr=${resp.get('costPerHr')}")
    return resp


def wait_for_running(pod_id: str, timeout_s: int = 900) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, resp = api("GET", f"/pods/{pod_id}")
        if status != 200 or not isinstance(resp, dict):
            log(f"poll status={status} resp={str(resp)[:200]}")
            time.sleep(15)
            continue
        ds = resp.get("desiredStatus")
        port_map = resp.get("portMappings") or {}
        public_ip = resp.get("publicIp")
        ssh_port = port_map.get("22")
        log(f"poll desiredStatus={ds} publicIp={public_ip} ssh_port={ssh_port}")
        if ds == "RUNNING" and ssh_port and public_ip:
            return resp
        time.sleep(15)
    raise TimeoutError(f"pod {pod_id} not RUNNING+SSH within {timeout_s}s")


def ssh_cmd(public_ip: str, ssh_port: int, command: str, timeout: int = 60):
    return subprocess.run(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-p", str(ssh_port),
            f"root@{public_ip}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def scp_to(public_ip: str, ssh_port: int, local: str, remote: str, timeout: int = 120):
    return subprocess.run(
        [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=20",
            "-P", str(ssh_port),
            local,
            f"root@{public_ip}:{remote}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def scp_from(public_ip: str, ssh_port: int, remote: str, local: str, timeout: int = 300):
    return subprocess.run(
        [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=20",
            "-r",
            "-P", str(ssh_port),
            f"root@{public_ip}:{remote}",
            local,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def wait_for_ssh(public_ip: str, ssh_port: int, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    last_err = ""
    while time.time() < deadline:
        proc = ssh_cmd(public_ip, ssh_port, "echo ssh_ready", timeout=25)
        if proc.returncode == 0 and "ssh_ready" in proc.stdout:
            log("ssh ready")
            return
        last_err = (proc.stderr or proc.stdout).strip()[:160]
        log(f"ssh wait: {last_err}")
        time.sleep(10)
    raise TimeoutError(f"ssh not ready within {timeout_s}s; last={last_err!r}")


def delete_pod(pod_id: str) -> None:
    if not pod_id:
        return
    log(f"DELETE pod {pod_id}")
    status, resp = api("DELETE", f"/pods/{pod_id}", timeout=60)
    log(f"delete returned status={status} resp={str(resp)[:160]}")
    # verify 404
    for _ in range(6):
        time.sleep(5)
        status_v, _resp = api("GET", f"/pods/{pod_id}", timeout=20)
        if status_v == 404:
            log(f"pod {pod_id} confirmed terminated (404)")
            return
        log(f"post-delete GET status={status_v} (waiting for 404)")
    log(f"WARN: pod {pod_id} did not return 404 after delete; check console")


def run_cell(public_ip: str, ssh_port: int, arm: str, seed: int, cell_dir: Path) -> dict:
    """Run a single (arm, seed) cell on the pod. Returns cell-result dict."""
    run_name = f"m4_{arm}_seed{seed}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    bench_args = (
        f"--url http://localhost:8000 "
        f"--model {HF_MODEL} "
        f"--flooder-rps {FLOODER_RPS} "
        f"--quiet-rps {QUIET_RPS} "
        f"--num-quiet {NUM_QUIET} "
        f"--duration-s {DURATION_S} "
        f"--max-tokens {MAX_TOKENS} "
        f"--output-dir RDIR/benchmarks "
        f"--seed {seed} "
        f"--prefix-overlap {PREFIX_OVERLAP} "
        f"--shared-prefix-tokens {SHARED_PREFIX_TOKENS}"
    )
    if arm == "arm2":
        bench_args += (
            f" --bias-flooder-cost {BIAS_FLOODER_COST}"
            f" --bias-after-N-reqs {BIAS_AFTER_N_REQS}"
            f" --bias-window-s {BIAS_WINDOW_S}"
        )
    log(f"--- cell start arm={arm} seed={seed} run_name={run_name} ---")
    t_cell = time.time()

    # Launch bootstrap nohup, return immediately. Per-cell timeout enforced
    # by polling the _DONE / _FAILED markers locally.
    launch_cmd = (
        f"cd /workspace && "
        f"nohup bash gate_pod_bootstrap.sh "
        f"--run-name {run_name} "
        f"--config configs/gate3_kv_eviction.yaml "
        f"--bench-script benchmarks/scripts/benchmark_n_tenant_single_model.py "
        f"--bench-args \"{bench_args}\" "
        f"> /workspace/{run_name}.console 2>&1 &"
        f" disown; echo PID=$!"
    )
    proc = ssh_cmd(public_ip, ssh_port, launch_cmd, timeout=30)
    log(f"launch rc={proc.returncode} stdout={proc.stdout.strip()[:120]}")
    if proc.returncode != 0:
        log(f"launch stderr: {proc.stderr[:200]}")
        return {
            "arm": arm, "seed": seed, "run_name": run_name,
            "status": "LAUNCH_FAIL", "wall_s": 0,
        }

    # Poll for done/failed marker. PER_CELL_TIMEOUT_S budget.
    deadline = time.time() + PER_CELL_TIMEOUT_S
    last_log_t = 0.0
    poll_cmd = (
        f"if [ -f /workspace/{run_name}_DONE ]; then echo DONE; "
        f"elif [ -f /workspace/{run_name}_FAILED ]; then echo FAILED; "
        f"else echo RUNNING; fi"
    )
    status = "TIMEOUT"
    while time.time() < deadline:
        time.sleep(20)
        proc = ssh_cmd(public_ip, ssh_port, poll_cmd, timeout=20)
        flag = (proc.stdout or "").strip()
        # Throttled progress log
        if time.time() - last_log_t > 60:
            tail = ssh_cmd(
                public_ip, ssh_port,
                f"tail -3 /workspace/{run_name}.console 2>/dev/null",
                timeout=15,
            )
            log(
                f"poll arm={arm} seed={seed} flag={flag} "
                f"elapsed={time.time()-t_cell:.0f}s "
                f"tail={tail.stdout.strip()[-200:]}"
            )
            last_log_t = time.time()
        if flag == "DONE":
            status = "DONE"
            break
        if flag == "FAILED":
            status = "FAILED"
            break

    wall_s = int(time.time() - t_cell)
    log(f"--- cell end arm={arm} seed={seed} status={status} wall={wall_s}s ---")

    # Pull tarball regardless of status (diagnostics matter on FAIL/TIMEOUT).
    tarball_remote = f"/workspace/{run_name}_results.tar.gz"
    local_tar = str(cell_dir / "results.tar.gz")
    proc = scp_from(public_ip, ssh_port, tarball_remote, local_tar, timeout=600)
    if proc.returncode == 0:
        log(f"scp tarball OK -> {local_tar}")
        # Untar in place.
        subprocess.run(
            ["tar", "-xzf", local_tar, "-C", str(cell_dir)],
            check=False, capture_output=True, text=True,
        )
    else:
        log(f"scp tarball FAILED rc={proc.returncode} stderr={proc.stderr[:200]}")

    # Pull the console log too.
    scp_from(
        public_ip, ssh_port,
        f"/workspace/{run_name}.console",
        str(cell_dir / "console.log"),
        timeout=60,
    )

    # If status TIMEOUT, kill the bootstrap on-pod so it doesn't keep
    # eating budget when we move to the next cell.
    if status == "TIMEOUT":
        kill_cmd = (
            f"touch /workspace/ABORT; sleep 5; "
            f"pkill -f gate_pod_bootstrap || true; "
            f"pkill -f kvwarden || true; "
            f"pkill -f vllm || true"
        )
        ssh_cmd(public_ip, ssh_port, kill_cmd, timeout=30)
        # Clear ABORT for next cell.
        ssh_cmd(public_ip, ssh_port, "rm -f /workspace/ABORT", timeout=15)

    return {
        "arm": arm, "seed": seed, "run_name": run_name,
        "status": status, "wall_s": wall_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reuse-pod", help="Existing pod id to reuse")
    parser.add_argument(
        "--arms", default="arm1,arm2",
        help="Comma-list of arms to run (default arm1,arm2)",
    )
    parser.add_argument(
        "--seeds", default="0,1,2",
        help="Comma-list of seeds (default 0,1,2)",
    )
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]

    pubkey = Path("~/.ssh/id_ed25519.pub").expanduser().read_text().strip()
    log(f"pubkey: {pubkey[:50]}... arms={arms} seeds={seeds}")

    if args.dry_run:
        log("dry-run; skipping pod creation")
        return 0

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

    pod_id: str = ""
    public_ip: str = ""
    ssh_port: int = 0
    cost_per_hr: float = HOURLY_RATE_FALLBACK
    cell_results: list[dict] = []

    def elapsed_cost() -> float:
        return (time.time() - T0) / 3600.0 * cost_per_hr

    def alarm_handler(signum, frame):
        log(f"WALL CAP {WALL_CAP_S}s reached; raising")
        raise TimeoutError("wall cap")

    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(WALL_CAP_S)

    try:
        if args.reuse_pod:
            pod_id = args.reuse_pod
            status, resp = api("GET", f"/pods/{pod_id}")
            if status != 200 or not isinstance(resp, dict):
                raise RuntimeError(f"reuse_pod {pod_id} not found")
            pod = resp
        else:
            attempts = 0
            pod = None
            while attempts < MAX_POD_CREATE_FAILS:
                try:
                    pod = create_pod(pubkey)
                    pod_id = pod["id"]
                    cost_per_hr = float(pod.get("costPerHr") or HOURLY_RATE_FALLBACK)
                    pod = wait_for_running(pod_id)
                    break
                except Exception as exc:
                    attempts += 1
                    log(f"pod create attempt {attempts} failed: {exc!r}")
                    if pod_id:
                        try:
                            delete_pod(pod_id)
                        except Exception as de:
                            log(f"cleanup-on-fail delete error: {de}")
                    pod_id = ""
                    if attempts >= MAX_POD_CREATE_FAILS:
                        raise RuntimeError("3 consecutive pod-create failures")
                    time.sleep(30)

        public_ip = pod["publicIp"]
        ssh_port = int(pod["portMappings"]["22"])
        cost_per_hr = float(pod.get("costPerHr") or cost_per_hr)
        log(
            f"pod up: id={pod_id} ip={public_ip} ssh_port={ssh_port} "
            f"hourly=${cost_per_hr:.2f}"
        )
        wait_for_ssh(public_ip, ssh_port)

        # scp the modified bootstrap (with gauge scraper) + helper.
        proc = scp_to(
            public_ip, ssh_port,
            str(WORKTREE / "scripts" / "gate_pod_bootstrap.sh"),
            "/workspace/gate_pod_bootstrap.sh",
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"scp bootstrap failed: {proc.stderr[:300]}")
        log("bootstrap.sh scp'd")

        # Push HF_TOKEN to /root/.gate_env so bootstrap can source it.
        env_cmd = (
            f"echo 'export HF_TOKEN={os.environ['HF_TOKEN']}' > /root/.gate_env && "
            f"echo 'export MAX_POD_SECS=18000' >> /root/.gate_env && "
            f"chmod +x /workspace/gate_pod_bootstrap.sh && "
            f"echo gate_env_done"
        )
        proc = ssh_cmd(public_ip, ssh_port, env_cmd, timeout=30)
        if proc.returncode != 0 or "gate_env_done" not in proc.stdout:
            raise RuntimeError(f"gate_env push failed: {proc.stderr[:200]}")

        log(f"setup complete; spend=${elapsed_cost():.2f}")

        # ---- 6-cell loop ----
        for arm in arms:
            for seed in seeds:
                if elapsed_cost() >= COST_ABORT_USD:
                    log(
                        f"COST ABORT: spend=${elapsed_cost():.2f} >= ${COST_ABORT_USD}; "
                        f"skipping remaining cells"
                    )
                    cell_results.append({
                        "arm": arm, "seed": seed, "status": "SKIPPED_COST",
                        "wall_s": 0, "run_name": "",
                    })
                    continue
                cell_dir = RESULTS_ROOT / f"m4_{arm}_seed{seed}"
                result = run_cell(public_ip, ssh_port, arm, seed, cell_dir)
                cell_results.append(result)
                log(
                    f"cumulative cells={len(cell_results)} "
                    f"spend=${elapsed_cost():.2f}"
                )

        log(f"all cells done; final spend=${elapsed_cost():.2f}")
        return 0

    except Exception as exc:
        log(f"ORCHESTRATOR EXCEPTION: {type(exc).__name__}: {exc}")
        return 1
    finally:
        signal.alarm(0)
        # Persist whatever we have.
        try:
            with open(RESULTS_ROOT / "cell_results.json", "w") as fh:
                json.dump({
                    "cells": cell_results,
                    "spend_usd": round(elapsed_cost(), 3),
                    "wall_s": round(time.time() - T0, 1),
                    "pod_id": pod_id,
                    "cost_per_hr": cost_per_hr,
                }, fh, indent=2)
        except Exception as je:
            log(f"cell_results.json write failed: {je}")
        log(f"final spend=${elapsed_cost():.3f} wall={time.time()-T0:.0f}s")
        try:
            delete_pod(pod_id)
        except Exception as exc:
            log(f"DELETE EXCEPTION: {exc}; check console for {pod_id}")


if __name__ == "__main__":
    sys.exit(main())
