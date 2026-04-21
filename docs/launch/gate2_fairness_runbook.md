# Gate 2-FAIRNESS Runbook

**Audience:** the human spending ~$3-4. Do these in order.
**Hardware:** 1× NVIDIA A100-SXM4 80GB on RunPod (SECURE on-demand, ~$1.89/hr).
**Wall:** ~1.5h expected for all 4 arms.
**Cost:** ~$3-4 expected, **$8 hard ceiling**. `MAX_POD_SECS=7200` per pod spin.
**main tip required:** `e3a5261` or later (PR #42, DRR priority wiring).

---

## Why Gate 2-FAIRNESS exists

Gate 1.5 robustly DISCONFIRMED the single-model admission cap hypothesis: vLLM's continuous batching matches a coarse upstream cap on TTFT (B/A=1.04× at c=256; A actively hurt at c=192). See `results/gate1_5_20260419/GATE1_5_OUTCOME.md`.

But engines have no concept of a **tenant**. If one user floods with 32 RPS and another tries to send 1 RPS, vLLM's scheduler will starve the quiet user because it optimizes aggregate throughput, not per-tenant fairness. This is a concrete problem that cannot be solved in-engine — it's structurally a middleware layer above.

Gate 2-FAIRNESS tests whether the DRR (Deficit Round-Robin) priority discipline shipped in PR #42 rescues the quiet tenant.

## Hypothesis

Under **flooder = 32 RPS, quiet = 1 RPS, same model, same A100**:

1. **Arm 0 (quiet only):** `quiet_user.p99_TTFT ≤ ~600ms` (steady-state solo baseline).
2. **Arm 1 (raw vLLM):** `quiet_user.p99_TTFT ≥ 4× Arm 0` — vLLM's scheduler starves the quiet tenant when a flooder shares the model.
3. **Arm 2 (KVWarden FIFO):** `quiet_user.p99_TTFT ≈ Arm 1` — current shipping scheduling doesn't help because under contention the queue is still dominated by the flooder's arrival rate.
4. **Arm 3 (KVWarden DRR):** `quiet_user.p99_TTFT ≤ 1.5× Arm 0` — DRR keeps the quiet tenant near-solo-baseline.

**CONFIRM rule (pre-committed):** `Arm 3.quiet.p99_TTFT ≤ 1.5 × Arm 0.quiet.p99_TTFT` AND `Arm 1.quiet.p99_TTFT ≥ 4 × Arm 0.quiet.p99_TTFT`.

**DISCONFIRM rule:** `Arm 3 > 2 × Arm 0` (DRR didn't help) OR `Arm 1 < 2 × Arm 0` (no starvation to fix — interesting in itself, means vLLM is fairer than expected).

## Pre-flight

1. **Local dress rehearsal passes.**
   ```bash
   bash scripts/gate2_fairness_dress_rehearsal.sh
   ```
   Look for `OVERALL: PASS`. If it fails, debug locally first — do not provision.

2. **HF_TOKEN valid for Llama.**
3. **RunPod balance ≥ $15.**
4. **A100-SXM4 SECURE spot price ≤ $2.50/hr.** If > $2.50, wait or switch region.
5. **Calendar block 2h.**

## Launch (5 commands — four arms + teardown)

### 1. Provision the A100-SXM4 pod

```bash
python3 - <<'EOF'
import os, time, runpod, json
runpod.api_key = os.environ["RUNPOD_API_KEY"]
pod = runpod.create_pod(
    name="kvwarden-gate2-fairness",
    image_name="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
    gpu_type_id="NVIDIA A100-SXM4-80GB",
    cloud_type="SECURE",
    container_disk_in_gb=80, volume_in_gb=0, gpu_count=1,
    ports="22/tcp,8000/http",
    env={"HF_TOKEN": os.environ["HF_TOKEN"], "MAX_POD_SECS": "7200"},
)
print(f"POD {pod['id']}")
# Poll runtime.ports for SSH info — see scripts/gate1_5 pattern.
EOF
```

Export `POD_IP` and `POD_PORT` from the printout. Smoke test:
```bash
ssh -p $POD_PORT root@$POD_IP 'nvidia-smi -L'
# Expect: GPU 0: NVIDIA A100-SXM4-80GB
```

### 2. Push env + bootstrap, run Arm 0 (quiet only, 240s for tighter p99)

```bash
cat > /tmp/.gate2_env <<EOF
export HF_TOKEN=$HF_TOKEN
export MAX_POD_SECS=7200
EOF
scp -P $POD_PORT /tmp/.gate2_env root@$POD_IP:/root/.gate_env
scp -P $POD_PORT scripts/gate_pod_bootstrap.sh root@$POD_IP:/workspace/

# Arm 0: flooder_rps=0, quiet_rps=1, duration=240s
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate2f_arm0_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate2_fairness_fifo.yaml \
  --bench-script benchmarks/scripts/benchmark_two_tenant_single_model.py \
  --bench-args "--url http://localhost:8000 --model meta-llama/Llama-3.1-8B-Instruct --flooder-rps 0 --quiet-rps 1 --duration-s 240 --max-tokens 128 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrap_arm0.console 2>&1 &
disown
REMOTE
```

Wait for `_DONE` marker, rsync.

### 3. Arm 1 — raw vLLM (bypass KVWarden admission)

Raw vLLM has no tenant concept. Run the bench against the vLLM engine port directly (KVWarden spawns on 8002+). First confirm the vLLM subprocess port from `server.log`.

Simpler: launch a fresh bootstrap with `max_concurrent: 99999` and `scheduling: fifo` — KVWarden becomes effectively a tenant-unaware passthrough. (Arm 1 is thus operationally `configs/gate2_fairness_fifo.yaml` with max_concurrent bumped to 99999; or use `--admission-bypass` if we grow that flag in a later PR.)

```bash
# Arm 1: flooder + quiet run together, FIFO scheduling (effectively no-op here)
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate2f_arm1_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate2_fairness_fifo.yaml \
  --bench-script benchmarks/scripts/benchmark_two_tenant_single_model.py \
  --bench-args "--url http://localhost:8000 --model meta-llama/Llama-3.1-8B-Instruct --flooder-rps 32 --quiet-rps 1 --duration-s 120 --max-tokens 128 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrap_arm1.console 2>&1 &
disown
REMOTE
```

**Note on Arm 1 vs Arm 2 separation:** with max_concurrent=256 and aggregate ~33 RPS, the admission cap likely doesn't bind at all in either arm — so Arm 1 and Arm 2 will look identical. That's OK; Arm 2 is a sanity check that no regression vs raw passthrough exists. The decisive delta is Arm 2 vs Arm 3.

### 4. Arm 2 — KVWarden FIFO (same as Arm 1 in practice)

Skip as separate run if you want — use Arm 1's numbers. Keep the arm in the runbook for experimental cleanliness: if your PR #42 accidentally regressed the `fifo` default path, Arm 2 would catch it.

### 5. Arm 3 — KVWarden DRR (the bet)

```bash
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate2f_arm3_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate2_fairness_drr.yaml \
  --bench-script benchmarks/scripts/benchmark_two_tenant_single_model.py \
  --bench-args "--url http://localhost:8000 --model meta-llama/Llama-3.1-8B-Instruct --flooder-rps 32 --quiet-rps 1 --duration-s 120 --max-tokens 128 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrap_arm3.console 2>&1 &
disown
REMOTE
```

Rsync, then **terminate the pod**.

## Reading the result

Per-arm `summary.json` has `quiet_user.ttft_p99_ms` and `flooder.ttft_p99_ms` and aggregate counts.

```
quiet_baseline = Arm0.quiet_user.ttft_p99_ms
raw_penalty    = Arm1.quiet_user.ttft_p99_ms / quiet_baseline
fifo_penalty   = Arm2.quiet_user.ttft_p99_ms / quiet_baseline
drr_penalty    = Arm3.quiet_user.ttft_p99_ms / quiet_baseline
```

### CONFIRM
- `drr_penalty ≤ 1.5` AND `raw_penalty ≥ 4`. DRR rescues a ≥4× starvation. Hero chart: 3 bars (Arm 0 / Arm 1 / Arm 3) + headline multiplier.

### DISCONFIRM (no starvation to fix)
- `raw_penalty < 2`. vLLM's scheduler was fairer than we expected. Publishable — "we tried to starve vLLM and couldn't, here's why that's interesting" + tells us middleware isn't needed for this workload shape.

### DISCONFIRM (DRR didn't help)
- `raw_penalty ≥ 4` AND `drr_penalty > 2`. Starvation exists, but DRR doesn't fix it. Indicates the queue isn't where the delay happens — probably engine-internal batching is the bottleneck. Next experiment: per-tenant KV isolation or request interleaving at the engine layer.

### PLUMBING REGRESSION (reject, don't interpret)
- Arm 2 differs meaningfully from Arm 1 → PR #42's FIFO default path regressed something. Debug.
- Arm 3 quiet tenant has higher p99 than flooder → priority inversion bug in `priority_score()` or the router wiring. Debug.

## Failure modes

| Symptom | Likely cause | Action |
|---|---|---|
| Engine pre-load > 10 min | A100 wheel cache cold | Wait up to 15 min; if no progress, `touch /workspace/ABORT` |
| `count_err > 5%` in any arm | Timeout, rate-limit, or OOM | Check `server.log` for `tenant_rejected` reason; raise `max_concurrent_requests` if budget exceeded |
| Arm 1 quiet p99 ≈ Arm 0 quiet p99 | No starvation observed — vLLM was fairer than expected OR flooder never loaded enough | Check flooder's summary: if `count_ok < 32 × 120 × 0.7 = 2688`, flooder underfired; bump `--flooder-rps 48` |
| Arm 3 quiet p99 > Arm 1 quiet p99 | DRR regressed (priority inversion?) | Check `src/kvwarden/tenant/manager.py::priority_score` — a sign flip would invert. Bail Gate 2-FAIRNESS, not a launch-blocker to debug |
| `/workspace/COST_CAP_HIT` | Pod exceeded MAX_POD_SECS | Terminate from RunPod console immediately |

## After Gate 2-FAIRNESS

1. Copy artifacts into `results/gate2_fairness_YYYYMMDD/`.
2. Write `GATE2_FAIRNESS_OUTCOME.md` with per-arm table + hero multiplier.
3. Update `PROGRESS.md` Gate 2-FAIRNESS section.
4. If CONFIRM → draft the launch post pivot ("Run two tenants on one A100 without K8s"). Launch target: Tue 2026-05-12.
5. If DISCONFIRM → consult advisor + god-planner on next experiment. Plan B: prompt-shape-aware scheduling. Plan C: ship Gate 2-lite-as-scoped.
