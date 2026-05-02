# Gate 3 — Cache-Pressure-Aware Admission Runbook

**Audience:** the operator spending ~$5-6 in M4 (probe) or ~$8 in M6 (full).
**Hardware:** 1× NVIDIA A100-SXM4 80GB on RunPod (SECURE on-demand, ~$1.50/hr at strategic-plan author time; verify at provision and update this runbook with the observed rate).
**Wall:** M4 probe = ~3 GPU-hours (6 cells × ~30 min). Full Gate 3 = ~4.5 GPU-hours (9 cells × ~30 min).
**Cost:** M4 ~$5-6, M6 ~$7-8, **$20 hard ceiling** (T2 budget).
**main tip required:** PR #115 signature stub merged + (for M6 only) PR-N M5a /metrics-poller wired; SHAs captured in OUTCOME at run time.

---

## Why Gate 3 exists

Gate 2-FAIRNESS proved DRR + token-bucket admission keeps quiet-tenant p99 TTFT near-solo under flooder pressure. Under sustained cache pressure the engine queues work that admission already cleared; the queueing tax shows up in TTFT, not in admission counters. Cache-pressure-aware admission scales flooder admission cost up when the engine reports high `vllm:kv_cache_usage_perc`, throttling the source before quiet tenants pay for it.

**Path C asks: at 32:1 RPS skew + 70% prompt prefix overlap + tight VRAM, does cache-pressure-aware admission beat DRR-only? What's the upper bound from a saturation-bias policy?** If Arm 2 vs Arm 1 < 1.2× on quiet-tenant p99 TTFT, the M5a real implementation cannot exceed it; T2 closes with a null result. Strategic plan: `~/Personal Projects/.claude/agent-memory-local/god-planner/project_kvwarden_t2_scope_apr28.md` REVISION.

## Hypothesis

Under capacity pressure regimes (vLLM `kv_cache_usage_perc` p99 ≥ ~0.7), cache-pressure-aware admission scales flooder-tenant priority cost up, reducing the queue depth into which quiet-tenant requests land. This compresses quiet-tenant p99 TTFT vs the DRR-only baseline.

1. **Arm 1 (DRR-only baseline):** quiet p99 TTFT degraded by flooder-induced engine queueing at high cache load. Admission processes the flooder at full DRR cadence; the engine absorbs the pressure and quiet TTFT inflates.
2. **Arm 2 (simulated saturation bias, M4 oracle):** flooder admission cost multiplied 4× during sustained pressure (>300 reqs in any 30s window). Bench-side bias bypasses the need for a /metrics poller. Measures the achievable upper bound.
3. **Arm 3 (real /metrics polling + cache-pressure admission, M6):** kvwarden polls `vllm:kv_cache_usage_perc`, scales flooder cost between soft (~0.7) and hard (~0.9) thresholds, saturates at 4× at hard. The implementation under test.

**Pass / disconfirm rubric.**

Stage 1 (M4, Arm 2 vs Arm 1, quiet-p99 TTFT median across 7 quiets):

| Delta | Action | v0.2.0 ship |
|---|---|---|
| ≥ 1.5× | (a) GA-track | Default-on, README hero |
| 1.2-1.5× | (a) experimental | Flag-gated, regime documented |
| < 1.2× | (c) disconfirm; queue (b) LMCache for v0.3 | DRR-only ships, null result published |
| Arm 2 worse than Arm 1 | Abandon (a) mid-flight; ship (c); queue (b) | Same as <1.2× row |

Stage 2 (M6, Arm 3 vs Arm 1, same threshold table). Arm 3 must track Arm 2 within ±15%; underperforming the simulated upper bound by more than 15% means the implementation needs a tuning pass before v0.2.0 ships.

**Single discriminating fact (kill criterion).** Does `vllm:kv_cache_usage_perc` p99 exceed ~0.7 in any arm of M4? If NO across regime-rescale attempts, no cache-pressure-conditioned policy can help — ship (c), queue (b).

**Metric of record:** per-tenant p99 TTFT aggregated across 7 quiets, reported as min/max/median. Headline = median. Max-of-7 catches priority inversion; min-of-7 catches one lucky tenant masking degradation.

## Cell matrix

3 arms × 3 seeds = **9 cells**, ~30 min/cell. **M4 probe = Arm 1 + Arm 2 = 6 cells = ~3 GPU-hours = ~$5-6.** **M6 full = 9 cells = ~4.5 GPU-hours = ~$7-8.** Combined M4 + M6 ~$13-14 vs $20 cap. Per-cell phases: pod provision ~5 min (first cell only), weights download ~10 min (first cell, cached after), vLLM cold-load ~2-3 min, bench warmup ~10 min (600s drain), bench run 5 min (300s sustained), rsync ~1 min.

## Pre-flight

1. **`vllm:kv_cache_usage_perc` gauge present (BLOCKING).** Before pod commit, scrape `/metrics` on a smoke vLLM container at the target version (0.19.1) and confirm `vllm:kv_cache_usage_perc` appears as a Gauge:
   ```bash
   curl -fsS http://localhost:8000/metrics | grep -E '^vllm:kv_cache_usage_perc'
   ```
   If absent the vLLM version is wrong (need ≥ 0.19.0; metric was introduced earlier but label shape stabilized at 0.19.x) — fix before proceeding. Then run a 60s smoke at the Gate 3 workload (N=8, 70% overlap, 32 RPS flooder) and confirm gauge p99 exceeds ~0.5 within any 60-second window. **If gauge p99 stays below ~0.5 the regime doesn't pressure cache and the experiment is regime-broken** — re-scale (longer prompts, more tenants, sustained flooder, or drop gmu to 0.35) before committing the full pod budget. Loop until either (a) gauge p99 > 0.5 in smoke, or (b) two regime-rescale attempts have failed — at which point ship (c) disconfirm without burning the full M4 budget.

2. **Arm 2 bias mechanism (BLOCKING for M4).** `benchmarks/scripts/benchmark_n_tenant_single_model.py` does not support an injected priority-bias flag today. Pick one before provisioning:
   - Land a `--bias-flooder-cost MULTIPLIER --bias-after-N-reqs N --bias-window-s S` triple on the harness (follow-up issue parallel to #120; outside this runbook's scope).
   - Bench-side mock: monkey-patch the harness to track flooder request count and inject a kvwarden admin-endpoint priority override on the threshold crossing.
   - Skip Arm 2, run Arm 1 only as a "DRR baseline-only" probe and ship (c) disconfirm by default — no upper bound was measured, so the M5a build is unjustified by absence of evidence.

3. **`--prefix-overlap` harness gap (BLOCKING; issue #120).** The N=8, 70%-overlap regime is what produces the cache pressure that exercises the cache-pressure-admission lever. The harness has `--prompt-length-dist` (Gate 2.2) but **NO `--prefix-overlap` flag**. The PROMPTS list is 8 short hardcoded strings with no shared prefix — running unmodified gives near-zero prefix-cache overlap. Pick one before provisioning:
   - Land `--prefix-overlap PCT --prefix-tokens N` on the harness (issue #120).
   - Bench-side mock: pre-generate a long shared prefix (~1500 tokens of a Wikipedia article); wrap each PROMPTS entry as `f"{shared_prefix}\n\nQuestion: {prompt}"`; monkey-patch the harness invocation.
   - Skip M4, push to M6 only.
   A 4.5h dry run on a no-overlap workload burns the entire budget without exercising the lever.

4. **Local dress rehearsal.** Run the smoke command in "Reproduce locally" — confirm YAML loads, kvwarden CLI accepts the config, mock-engine smoke produces summary.json. Don't provision if it fails.

5. **HF_TOKEN, RunPod balance, GPU spot price, calendar.** `curl -fsS -H "Authorization: Bearer $HF_TOKEN" https://huggingface.co/api/models/meta-llama/Llama-3.1-8B-Instruct` returns 200. RunPod balance ≥ $25 (3× expected; pod retries common on SECURE). A100-SXM4 80GB SECURE spot ≤ $2.00/hr (else wait or switch region). M4 = 4h block, M6 = 5h block.

## Launch

### 1. Provision

```bash
runpodctl create pod \
  --name "kvwarden-gate3-cache-pressure-admission" \
  --gpu-type "A100 SXM4 80GB" \
  --image runpod/pytorch:2.1.0-py3.10-cuda12.1.1-devel-ubuntu22.04 \
  --cloud-type SECURE \
  --container-disk-gb 80 \
  --ports "22/tcp,8000/http" \
  --env "HF_TOKEN=$HF_TOKEN" \
  --env "MAX_POD_SECS=21600"
```

**Why `runpod/pytorch` not `vllm/vllm-openai` (P1 finding 2026-05-02):** the `vllm/vllm-openai` image runs vLLM as PID 1 with no sshd. Bench orchestration here SSHes in, runs `gate_pod_bootstrap.sh` to pip-install vLLM, then launches engines as subprocesses. The pytorch base gives sshd + room for `pip install vllm==0.19.1` during bootstrap. The Compose bundle (#126) uses `vllm/vllm-openai` because it talks to the engine over HTTP only, no SSH needed.

(MAX_POD_SECS=21600 = 6h, above 4.5h compute budget. In-pod self-destruct is best-effort; the operator's calendar alarm is the actual ceiling.)

Capture `POD_IP`, `POD_PORT`. Smoke: `ssh -p $POD_PORT root@$POD_IP 'nvidia-smi -L'` should print the A100.

### 2. Push env + bootstrap

Push `HF_TOKEN`, `MAX_POD_SECS=21600` to `/root/.gate_env`; scp `scripts/gate_pod_bootstrap.sh` to `/workspace/` on the pod.

### 3. Run cells

For each `(ARM, SEED)` in the loop:

```bash
ssh -p $POD_PORT root@$POD_IP <<REMOTE
nohup bash /workspace/gate_pod_bootstrap.sh \
  --run-name gate3_${ARM}_seed${SEED}_\$(date -u +%Y%m%d_%H%M%S) \
  --config configs/gate3_kv_eviction.yaml \
  --bench-script benchmarks/scripts/benchmark_n_tenant_single_model.py \
  --bench-args "--url http://localhost:8000 --model meta-llama/Llama-3.1-8B-Instruct --flooder-rps 32 --quiet-rps 1 --num-quiet 7 --duration-s 300 --max-tokens 128 --output-dir RDIR/benchmarks --seed ${SEED}" \
  > /workspace/bootstrap_${ARM}_${SEED}.console 2>&1 &
disown
REMOTE
```

Wait for `_DONE`, rsync the cell artifacts (including `prometheus_dump.txt` for the gauge distribution), repeat.

**M4 probe loop:** `{arm1, arm2} × {0, 1, 2}` = 6 cells.
**M6 full loop:** `{arm1, arm2, arm3} × {0, 1, 2}` = 9 cells.

### 4. Teardown

```bash
runpodctl remove pod $POD_ID
```

Verify in console.

## Reading the result

Each cell's `summary.json` carries `quiet_per_tenant.quiet_N.ttft_p99_ms`. Headline = median across 7 quiets of per-tenant p99, then median across 3 seeds. Delta = `arm1_quiet_p99_med / arm2_quiet_p99_med`. Parse `prometheus_dump.txt` per cell for `vllm:kv_cache_usage_perc` p50/p95/p99. Apply the rubric and the kill criterion.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| OOM on engine cold-load | gmu=0.40 too tight | Drop to `gpu_memory_utilization: 0.35`; note in OUTCOME |
| `vllm:kv_cache_usage_perc` gauge absent from `/metrics` | vLLM minor renamed the metric (label/name drift) | Grep `/metrics` for `kv_cache` and `cache_usage` variants; pin a newer image if needed |
| `/metrics` scrape latency > 200 ms | Engine is hot, scrape contention | Increase TTL on kvwarden poller to 2s; if persists, drop scrape rate |
| Gauge p99 < 0.5 across all arms in smoke | Workload doesn't pressure cache (regime broken) | Re-scale per pre-flight item 1; after two failed rescales, ship (c) |
| All quiet tenants identical p99 | Prefix-overlap gap — no shared prefix | Re-do pre-flight item 3 |
| Arm 2 quiet p99 > Arm 1 quiet p99 | Bias mock broken or regime noise > 4× signal | Verify bench-side bias took (server.log priority traces); re-check gauge p99 |
| Arm 3 underperforms Arm 2 by > 15% | M5a poller stale or threshold mistuned | Inspect kvwarden /metrics for stale snapshots; re-tune soft/hard against M4 gauge distribution |
| Flooder count_err > 5% | Token-bucket fired (expected at 32 RPS w/ rate_limit_rpm=600) | Confirm errors are 429s in CSV; if timeouts, raise `--timeout-s 90` |
| Quiet count_err > 0% | Plumbing regression (admission rejecting quiet) | Check `tenant_rejected` in server.log; debug, don't interpret |

## Reproduce locally

CPU smoke against the mock engine (60s, no GPU):

```bash
python -m benchmarks.scripts.benchmark_n_tenant_single_model \
  --url http://localhost:8000 \
  --model meta-llama/Llama-3.1-8B-Instruct \
  --flooder-rps 32 --quiet-rps 1 --num-quiet 7 \
  --duration-s 60 \
  --max-tokens 32 \
  --output-dir /tmp/gate3_smoke \
  --seed 0
```

Pre-launch `kvwarden serve --config configs/gate3_kv_eviction.yaml` in another terminal first; mock engine at `benchmarks/scripts/mock_engine.py`. Full run uses `--duration-s 300`. Smoke does NOT exercise the cache-pressure gauge; that requires real vLLM.

## Provenance

After all cells complete, archive to `results/gate3_TBD/` (rename to `results/gate3_YYYYMMDD/` on run day):

- `arm{1,2,3}_seed{0,1,2}/` — per-cell bundles: `summary.json`, `tenant_flooder.csv`, `tenant_quiet_{0..6}.csv`, `bench.log`, `server.log`, `engine_logs/`, `gpu_trace.csv`, `prometheus_dump.txt` (source of truth for the cache-pressure gauge distribution), `status_{before,after}.json`, `pip_freeze.txt`, `git_head.txt`.
- `OUTCOME.md` — fill the pre-landed template at `results/gate3_TBD/OUTCOME.md`. The OUTCOME captures the gauge distribution (p50/p95/p99 per arm) explicitly — it is the kill-criterion evidence.
- `summarize_gate3.py` — post-warmup percentile re-extractor plus a parser that pulls `vllm:kv_cache_usage_perc` percentiles out of each cell's `prometheus_dump.txt`.

## After Gate 3

1. Copy artifacts into `results/gate3_YYYYMMDD/`. Fill `OUTCOME.md` including the cache-pressure regime check. Update `PROGRESS.md` with the M4 (or M6) decision.
2. Per the rubric:
   - **≥ 1.5×** → kick off M5a if not started; ship default-on in v0.2.0. Update issue #103.
   - **1.2-1.5×** → ship M5a flag-gated; document the regime in v0.2.0 notes.
   - **< 1.2× or Arm 2 worse than Arm 1 or gauge p99 < 0.7** → draft (c) disconfirm; park M5a; queue (b) LMCache for v0.3. Strategic plan REVISION.
