# Gate 1 Launch Runbook

**Audience:** the human spending the $7-10. Do these in order. Don't skip the pre-flight.

**Hardware:** 1× NVIDIA H100 SXM5 80GB on RunPod (SECURE pod).
**Wall:** ~1.8h.
**Cost:** ~$5-6 expected on H100 SXM5 SECURE on-demand at $2.99/hr × ~1.8h. The `MAX_POD_SECS=10800` (3h) timer caps pod-side bootstrap effort, but **the in-pod `poweroff` call is best-effort** — RunPod containers usually don't grant `SYS_BOOT`, so the actual cost ceiling depends on YOU manually terminating the pod from the RunPod console after the timer fires (the pod writes `/workspace/COST_CAP_HIT` as a marker; poll for it). Treat in-pod cost-cap as defense in depth, not the primary control. **Set yourself a calendar alarm at 2h after pod creation as the real ceiling.**
**main tip required:** `5b64b4c` or later.

---

## Pre-flight (do not skip — each check has burned budget historically)

1. **Source of truth for fixes.** `git -C ~/Personal\ Projects/kvwarden_implementation_poc/kvwarden log --oneline -10` should show PR #28 through #35 in `main`.
2. **Local CPU dress rehearsal exits PASS.**
   ```bash
   cd ~/Personal\ Projects/kvwarden_implementation_poc/kvwarden
   NUM_REQUESTS=300 SKIP_DISCRIMINATOR=1 bash scripts/gate1_dress_rehearsal.sh
   ```
   Look for `OVERALL: PASS — Gate 1 plumbing is wired correctly`. If it fails, **do not provision the pod.** Debug locally first.
3. **HF_TOKEN is valid for Llama.**
   ```bash
   curl -fsS -H "Authorization: Bearer $HF_TOKEN" \
     https://huggingface.co/api/models/meta-llama/Llama-3.1-8B-Instruct \
     | head -c 200
   ```
   If this 401s, fix HF access before provisioning.
4. **RunPod balance ≥ $25.** (3× expected spend; spot pods may need to reschedule.)
5. **H100 SXM5 spot price.** Check the RunPod console; if > $4/hr, wait or switch to a different region.
6. **Calendar block 2.5h.** First 30 min are the critical window — engine bring-up is the historical failure mode.

---

## Launch (3 commands)

Per advisor: **don't** bundle these into a one-button script. The first time, run them by hand so a failure in step N doesn't auto-trigger steps N+1.

### 1. Provision the pod

```bash
cd ~/Personal\ Projects/kvwarden_implementation_poc/kvwarden
python3 scripts/provision_runpod.py \
  --gpu "NVIDIA H100 80GB HBM3" \
  --ports "22/tcp,8000/http" \
  --image "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04" \
  --tag gate1
```

Capture the pod IP and the SSH port. Test:
```bash
ssh -p $POD_PORT root@$POD_IP 'nvidia-smi -L'
# Expect: GPU 0: NVIDIA H100 80GB HBM3 (UUID: ...)
```

### 2. Push env + bootstrap, run Arm A

```bash
# Local: prep env file with secrets
cat > /tmp/.gate1_env <<EOF
export HF_TOKEN=$HF_TOKEN
EOF

scp -P $POD_PORT /tmp/.gate1_env root@$POD_IP:/root/.gate_env
scp -P $POD_PORT scripts/gate_pod_bootstrap.sh root@$POD_IP:/workspace/

# Run Arm A in the background (admission ON, max_concurrent=128)
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate1A_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate1_admission.yaml \
  --bench-script benchmarks/scripts/benchmark_multi_model.py \
  --bench-args "--url http://localhost:8000 --models meta-llama/Llama-3.1-8B-Instruct --workload concurrent --concurrency 128,256 --num-requests 400 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrap.console 2>&1 &
disown
REMOTE
```

The pod's 3h self-destruct timer is now armed. To watch progress:
```bash
ssh -p $POD_PORT root@$POD_IP 'tail -f /workspace/bootstrap.log'
```

To abort cleanly (results still get tarred):
```bash
ssh -p $POD_PORT root@$POD_IP 'touch /workspace/ABORT'
```

When you see `=== Bootstrap end: ... status=DONE ===`, rsync the results:
```bash
mkdir -p results/gate1_$(date -u +%Y%m%d)/armA
rsync -avz -e "ssh -p $POD_PORT" \
  root@$POD_IP:/workspace/gate1A_*_results.tar.gz \
  results/gate1_$(date -u +%Y%m%d)/armA/
```

### 3. Repeat for Arm B (admission OFF, same pod)

```bash
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate1B_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate1_admission_off.yaml \
  --bench-script benchmarks/scripts/benchmark_multi_model.py \
  --bench-args "--url http://localhost:8000 --models meta-llama/Llama-3.1-8B-Instruct --workload concurrent --concurrency 128,256 --num-requests 400 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrapB.console 2>&1 &
disown
REMOTE
```

Rsync Arm B results, then **terminate the pod** in the RunPod console.

---

## Reading the result

For each arm, parse `concurrent_c{128,256}_summary.json::ttft_p99_ms` from the rsynced tarballs. Compute:

```
A_ratio = A_p99(c=256) / A_p99(c=128)
B_vs_A  = B_p99(c=256) / A_p99(c=256)
```

### Hypothesis CONFIRMED if:
- `A_ratio ≤ 2` — admission flattens the cliff
- `B_vs_A ≥ 4` — uncapped engine hits the cliff hard

→ Write up as Gate 1 PASS, ship the results-driven launch post.

### Hypothesis DISCONFIRMED if:
- Both arms produce near-identical TTFTs across c=128/256

→ This is **a real negative result, not a plumbing bug**. The c=128→c=256 cliff Phase 1 saw (185ms → 1293ms, 7×) may have been an artifact of the pre-PR-#28 TTFT bug (which timed SSE-frame RTT, sensitive to system load in ways real first-token isn't). Publishable as: "we instrumented honestly and the cliff didn't reproduce; admission's value at this scale is smaller than projected."

### Plumbing sanity (look at this BEFORE deciding which of the above):
- Arm A `prometheus_dump.txt` should show `kvwarden_admission_in_flight` near 128 at c=256 and `kvwarden_admission_queue_depth > 0` for sustained periods. If both are 0, admission isn't engaging — file an issue, don't trust the TTFT numbers.
- Arm B `prometheus_dump.txt` should show `kvwarden_admission_in_flight` reaching 256+. If clamped at 100 or 128, the connector limit (PR #34) regressed.

---

## Failure modes

| Symptom | Likely cause | Action |
|---|---|---|
| `Failed to pre-load model` in `server.log` within 5 min | HF_TOKEN missing or invalid on pod | Re-scp `/root/.gate_env`, restart bootstrap |
| Engine bring-up exceeds 90 min | OOM during model load OR vLLM v1 incompatibility | Check `/workspace/results/*/engine_logs/`. PR #16's `VLLM_USE_V1=0` should already be set; if OOM, lower `gpu_memory_utilization` in the config |
| Cost-cap timer fires (`COST CAP HIT` in bootstrap.log, `/workspace/COST_CAP_HIT` exists) | Script took longer than `MAX_POD_SECS=10800` | **Manually terminate from the RunPod console** — in-pod poweroff usually fails on containerized pods. Bundle is already tarred (trap fired before the timer's poweroff attempt). Rsync results, then terminate. |
| Phase 4 silent-hang (no log progress for 10+ min, but `bootstrap.log` not advancing) | vLLM engine stuck during weight load — happened in Gate 0.5 | `ssh pod 'tail -n 200 /workspace/results/*/engine_logs/*.stderr'` for the actual stack. Common causes: model download stalled (HF rate limit), CUDA context init deadlock, or KV cache sizing OOM. If no progress in 15 min, `ssh pod 'touch /workspace/ABORT'` to clean-bundle and exit. |
| Bench reports many 429s | `tenant_defaults` regression. Should be 1024 max_concurrent_requests in the gate1 yaml | `grep tenant_defaults configs/gate1_admission*.yaml` to verify |
| All 4 ttft_p99 values come back as -1 | Bench summary JSON malformed | Read the bench.log inside the tarball; PR #34's R5 phase-abort may have triggered |

---

## After Gate 1

1. Copy raw artifacts into `results/gate1_$(date)/`.
2. Write `GATE1_OUTCOME.md` (template: copy gate06_20260419/GATE06_OUTCOME.md, swap numbers).
3. Update `CORRECTIONS.md` if any new caveats surfaced.
4. Update `PROGRESS.md` Gate 1 section.
5. Decide on the launch post (PR #20 DRAFT) — Gate 0 vs Gate 1 framing.

---

## Gate 1.5 Re-Run — Powered version of Gate 1 (added 2026-04-19)

**Why:** Gate 1 ran ~10s wall per `(arm × concurrency)` cell. By Little's Law, Arm A c=128 only averaged ~103 in-flight (BELOW cap=128), and only 272/801 admits queued (~34%). The headline B/A=1.04× was short-bench under-power, **not** a real DISCONFIRM. See `results/gate1_20260419/GATE1_OUTCOME.md` "Methodology caveat" section. Gate 1.5 re-runs the same hypothesis with 10× the sample size to actually exercise sustained cap pressure.

**Hardware:** 1× NVIDIA H100 SXM5 80GB on RunPod (SECURE pod), $2.99/hr.
**Wall:** ~2.5h expected.
**Cost:** ~$7-10 expected; **$15 hard ceiling**.
**main tip required:** `99935b3` or later (PR #39 caveat in OUTCOME doc).

### Pre-flight delta vs Gate 1

Same pre-flight as Gate 1 above, plus:

7. **Confirm Gate 1.5 budget posture.** $15 hard ceiling, calendar alarm at 2.5h.
8. **Decide Arm A concurrency steps before launch.** Default plan: `64,128,192,256`. If c=64 cell takes >18 min in Arm A, downgrade to `64,128,256` only and rerun (see "Abort-early rules" below).

### Launch (4 commands — one extra for the inter-arm pod restart)

#### 1. Provision the pod (same as Gate 1, with `MAX_POD_SECS=5400`)

```bash
cd ~/Personal\ Projects/kvwarden_implementation_poc/kvwarden
python3 scripts/provision_runpod.py \
  --gpu "NVIDIA H100 80GB HBM3" \
  --ports "22/tcp,8000/http" \
  --image "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04" \
  --tag gate1_5
```

Capture pod IP, SSH port, and **pod_id** (you will need pod_id for the inter-arm restart).

#### 2. Push env + bootstrap, run Arm A (note `MAX_POD_SECS=5400`)

```bash
cat > /tmp/.gate1_5_env <<EOF
export HF_TOKEN=$HF_TOKEN
export MAX_POD_SECS=5400
EOF

scp -P $POD_PORT /tmp/.gate1_5_env root@$POD_IP:/root/.gate_env
scp -P $POD_PORT scripts/gate_pod_bootstrap.sh root@$POD_IP:/workspace/

ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate1_5A_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate1_admission.yaml \
  --bench-script benchmarks/scripts/benchmark_multi_model.py \
  --bench-args "--url http://localhost:8000 --models meta-llama/Llama-3.1-8B-Instruct --workload concurrent --concurrency 64,128,192,256 --num-requests 4000 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrap.console 2>&1 &
disown
REMOTE
```

When `=== Bootstrap end: ... status=DONE ===`, rsync:

```bash
mkdir -p results/gate1_5_$(date -u +%Y%m%d)/armA
rsync -avz -e "ssh -p $POD_PORT" \
  root@$POD_IP:/workspace/gate1_5A_*_results.tar.gz \
  results/gate1_5_$(date -u +%Y%m%d)/armA/
```

#### 3. **CRITICAL — restart the pod between arms** (this is what Gate 1 missed)

Gate 1 documented (`GATE1_OUTCOME.md` line 90) that Arm A's vLLM subprocess leaked 67.9 GiB of GPU memory past `pkill -9`, and `nvidia-smi --gpu-reset` is "Not Supported" on containerized pods. Arm B then CUDA-OOM'd on engine pre-load. **Recovery required `runpod.stop_pod + resume_pod`**. Bake this in:

```bash
python3 - <<EOF
import runpod, os, time
runpod.api_key = os.environ["RUNPOD_API_KEY"]
runpod.stop_pod("$POD_ID")
time.sleep(60)   # wait for stop to settle (45s was too short in practice)
# IMPORTANT: runpod SDK 1.9+ requires gpu_count as a kwarg to resume_pod.
# Calling resume_pod(pod_id) alone raises TypeError.
runpod.resume_pod("$POD_ID", gpu_count=1)
EOF
```

Wait ~90s for SSH to come back. **The pod's `/workspace` is on container overlay fs — contents are LOST on stop+resume.** Re-scp env + bootstrap:

```bash
# SSH port may have changed after resume — re-fetch from RunPod console
export POD_PORT=$NEW_PORT

scp -P $POD_PORT /tmp/.gate1_5_env root@$POD_IP:/root/.gate_env
scp -P $POD_PORT scripts/gate_pod_bootstrap.sh root@$POD_IP:/workspace/

# Confirm GPU clean before launching Arm B
ssh -p $POD_PORT root@$POD_IP 'nvidia-smi --query-gpu=memory.used --format=csv,noheader'
# Expect: 0 MiB
```

#### 4. Run Arm B (admission OFF, fresh GPU state)

```bash
ssh -p $POD_PORT root@$POD_IP <<'REMOTE'
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate1_5B_$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate1_admission_off.yaml \
  --bench-script benchmarks/scripts/benchmark_multi_model.py \
  --bench-args "--url http://localhost:8000 --models meta-llama/Llama-3.1-8B-Instruct --workload concurrent --concurrency 64,128,192,256 --num-requests 4000 --output-dir RDIR/benchmarks --seed 42" \
  > /workspace/bootstrapB.console 2>&1 &
disown
REMOTE
```

Rsync Arm B results, then **terminate the pod** in the RunPod console.

### Abort-early rules (Gate 1.5-specific)

| Trigger | Action |
|---|---|
| c=64 cell takes > 18 min in Arm A | Abort, drop c=192 from the args, restart Arm A with `64,128,256` only |
| Engine bring-up > 15 min, no log progress | `ssh pod 'touch /workspace/ABORT'` |
| `kvwarden_admission_in_flight` stuck at 0 during c=256 (Arm A) | Admission isn't engaging — file an issue, do not run Arm B |
| Arm A wall-clock projected > 75 min | Abort — hypothesis isn't admission-cliff-shaped at this concurrency profile |
| `/workspace/COST_CAP_HIT` appears | Manually terminate from RunPod console immediately |

### Reading the Gate 1.5 result

Three outcome buckets, all publishable:

1. **CONFIRM** — `A_p99(c=256) ≤ 2 × A_p99(c=128)` AND `B_p99(c=256) ≥ 4 × A_p99(c=256)`. Original hypothesis survives. Ship the launch post on this number.
2. **Robust DISCONFIRM** — both arms produce near-identical TTFTs across all concurrency steps even at sustained pressure. Admission genuinely doesn't help tail TTFT on this workload shape. Pivot pitch to multi-model arbitration (Gate 2-lite) or queue-wait-spike avoidance.
3. **Overload-protection emergence** — Arm B's vLLM degenerates (queues to OOM, returns 500s, or TTFT explodes >10×) at some concurrency level; Arm A stays graceful. This is a different and arguably stronger story than "Arm A has lower p99": "we prevent the engine from melting under load."

### After Gate 1.5

1. Copy artifacts into `results/gate1_5_$(date)/`.
2. Write `GATE1_5_OUTCOME.md` with Little's Law analysis baked in (avg in-flight per cell, queue-engagement %).
3. Update `PROGRESS.md` Gate 1.5 section + the strategic plan.
4. Decide launch-post framing per outcome bucket above.
