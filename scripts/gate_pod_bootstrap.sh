#!/bin/bash
# Generic InferGrid Gate-run pod bootstrap (Gate 0.6, 1, 2, 3+).
# Idempotent. Parameterized. Bundles results+engine_logs ON ANY EXIT (D2 fix).
#
# 5 Gate-0 lessons baked in here (see results/gate0_20260418/GATE0_OUTCOME.md):
#   1. SSH port mapping --> handled upstream by scripts/provision_runpod.py.
#   2. HF_TOKEN env --> sourced from /root/.gate_env (scp'd by master).
#   3. transformers<5 --> asserted in dep verification (PR #16 pin).
#   4. numpy<2.3      --> asserted in dep verification (PR #16 pin).
#   5. vLLM v1 OOM at co-load --> VLLM_USE_V1=0 here; gpu_mem_util in config.
#
# Scope: this script ONLY runs ON the pod. It does not provision, tear down,
# or rsync results out -- master handles those.
#
# Usage:
#   nohup bash gate_pod_bootstrap.sh \
#       --run-name gate1_20260420_1530 \
#       --config configs/gate1_admission.yaml \
#       --bench-script benchmarks/scripts/benchmark_multi_model.py \
#       --bench-args "--url http://localhost:8000 --concurrency 1,8,32 --num-requests 50 --output-dir RDIR/benchmarks --seed 42" \
#       > /workspace/bootstrap.console 2>&1 &
#
# --bench-script "" skips Phase 7 (serve-only runs).
# RDIR placeholder in --bench-args is substituted with the run results dir.

set -u
exec > >(tee -a /workspace/bootstrap.log) 2>&1

# ---- Cost-cap defense in depth (3 layers) ----
# Spot H100 SXM5 burns ~$4/hr; an unattended runaway pod is the dominant
# overspend risk. Three independent controls so any one failing still saves
# the budget.
#
# Layer 1: self-destruct timer. After MAX_POD_SECS (default 10800s = 3h ≈
# $12 ceiling), the pod tries to halt itself. KNOWN LIMITATION: containerized
# pods (RunPod default) almost never grant SYS_BOOT, so `poweroff`/`halt` are
# no-ops. We try them anyway, plus `kill -KILL -1` to take down PID 1, and
# write a STARK marker file `/workspace/COST_CAP_HIT` that the master should
# poll for. The runbook (docs/launch/gate1_runbook.md) instructs the operator
# to manually terminate the pod from the RunPod console if this marker shows
# up — this is the ACTUAL cost ceiling. The in-pod attempts are defense in
# depth, not the primary control.
MAX_POD_SECS="${MAX_POD_SECS:-10800}"
(
  sleep "$MAX_POD_SECS"
  {
    echo "==================================================================="
    echo "=== !!! COST CAP HIT: $MAX_POD_SECS seconds elapsed at $(date -u) !!! ==="
    echo "=== Pod must be terminated MANUALLY from RunPod console if these  ==="
    echo "=== self-destruct attempts fail (likely on a containerized pod).  ==="
    echo "==================================================================="
  } >> /workspace/bootstrap.log 2>&1
  # Touch the marker FIRST so master-side polling can see it before any
  # in-pod actions land or fail.
  touch /workspace/COST_CAP_HIT 2>/dev/null || true
  # Also touch the abort sentinel so a clean trap can still bundle results.
  touch /workspace/ABORT 2>/dev/null || true
  # Give the trap 30s to tar 200MB+ before we try to take the pod down.
  # 5s was borderline; 30s costs ~$0.025 and protects the diagnostics.
  sleep 30
  poweroff 2>/dev/null || halt 2>/dev/null || systemctl poweroff 2>/dev/null \
    || kill -KILL -1 2>/dev/null || true
) &
COSTCAP_PID=$!
echo "cost-cap timer pid=$COSTCAP_PID max_pod_secs=$MAX_POD_SECS"
echo "cost-cap marker file: /workspace/COST_CAP_HIT (master should poll)"

# Layer 2: manual ABORT sentinel. User can `ssh pod 'touch /workspace/ABORT'`
# to cleanly trigger the existing bundle_and_mark trap. Checked at every
# polling boundary below.
abort_check() {
  if [ -f /workspace/ABORT ]; then
    echo "=== /workspace/ABORT found — aborting at $(date -u) ==="
    fail "ABORT sentinel"
  fi
}

# Layer 3: phase wall-clock budget. Engine pre-load is the historical
# failure mode that eats budget (Gate 0 ran 6 retries). If Phase 4
# (serve start) → now exceeds MAX_POD_SECS/2, we abort BEFORE running the
# bench: a 90+min engine startup means something is wrong; do not also
# spend an hour benching a broken setup. Per-phase timestamps written to
# RDIR/phase_*.ts; checked at the top of Phase 7.

# ---- Args ----
RUN_NAME=""; CONFIG=""; BENCH_SCRIPT=""; BENCH_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --run-name)     RUN_NAME="$2"; shift 2 ;;
    --config)       CONFIG="$2"; shift 2 ;;
    --bench-script) BENCH_SCRIPT="$2"; shift 2 ;;
    --bench-args)   BENCH_ARGS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -z "$RUN_NAME" ] && RUN_NAME="gate_$(date -u +%Y%m%d_%H%M%S)"
[ -z "$CONFIG" ] && { echo "FATAL: --config required" >&2; exit 2; }

RDIR=/workspace/results/$RUN_NAME
ELOG=$RDIR/engine_logs
TARBALL=/workspace/${RUN_NAME}_results.tar.gz
STATUS=FAILED   # flipped to DONE only at very end

mkdir -p "$ELOG"
echo "=== Bootstrap start: $(date -u) RUN_NAME=$RUN_NAME ==="

# ---- Single EXIT trap = D2 fix (covers fail, SIGTERM, kill, success) ----
bundle_and_mark() {
  local rc=$?
  echo "--- trap: bundling (status=$STATUS rc=$rc) ---"
  # Kill the cost-cap subshell first. Without this, a normal-exit run
  # leaves the timer counting and it eventually fires mid-rsync (or worse,
  # after the master has terminated the pod, but RunPod restarted it).
  if [ -n "${COSTCAP_PID:-}" ]; then
    kill "$COSTCAP_PID" 2>/dev/null || true
  fi
  kill "$(cat $RDIR/server.pid 2>/dev/null)"     2>/dev/null || true
  kill "$(cat $RDIR/gpu_trace.pid 2>/dev/null)" 2>/dev/null || true
  sleep 2; pkill -9 -f "vllm.entrypoints" 2>/dev/null || true
  cp /workspace/bootstrap.log "$RDIR/bootstrap.log" 2>/dev/null || true
  tar -czf "$TARBALL" -C /workspace "results/$RUN_NAME" 2>/dev/null \
    && echo "archive: $(du -h $TARBALL | cut -f1)" \
    || echo "WARN: tar failed"
  if [ "$STATUS" = "DONE" ]; then touch /workspace/${RUN_NAME}_DONE
  else echo "rc=$rc" > /workspace/${RUN_NAME}_FAILED; fi
  echo "=== Bootstrap end: $(date -u) status=$STATUS ==="
}
trap bundle_and_mark EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }   # trap still fires

# ---- Env (HF_TOKEN etc.) ----
[ -f /root/.gate_env ] && { set -a; . /root/.gate_env; set +a; echo "loaded /root/.gate_env"; }
export VLLM_USE_V1=0
export PYTHONUNBUFFERED=1
export INFERGRID_ENGINE_LOG_DIR="$ELOG"   # PR #16 per-engine stderr capture
[ -n "${HF_TOKEN:-}" ] || fail "HF_TOKEN not set"

# Phase timestamp helper (for layer-3 wall-clock budget).
phase_ts() { date +%s > "$RDIR/phase_$1.ts"; }

# ---- Phase 1: apt + clone main ----
phase_ts 1
echo "--- Phase 1: apt + clone ---"
apt-get update -qq && apt-get install -y -qq git rsync curl jq || fail "apt install"
cd /workspace; rm -rf infergrid
git clone --branch main --depth 1 https://github.com/coconut-labs/infergrid.git || fail "git clone"
cd infergrid; echo "main HEAD: $(git rev-parse --short HEAD)  $(git log -1 --pretty=%s)"
abort_check

# ---- Phase 2: python deps ----
phase_ts 2
echo "--- Phase 2: python deps ---"
python3 -m pip install --quiet --upgrade pip || fail "pip upgrade"
python3 -m pip install --quiet 'vllm==0.8.5' || fail "vllm install"
python3 -m pip install --quiet -r requirements-gpu.txt || fail "requirements-gpu"
python3 -m pip install --quiet -e . || fail "infergrid editable"
python3 -m pip install --quiet 'huggingface_hub[cli]' || fail "hf cli"

# ---- Dep verification (Gate-0 lessons 3+4) ----
python3 -c "
import transformers, numpy, vllm, torch
print(f'vllm={vllm.__version__} transformers={transformers.__version__} numpy={numpy.__version__} torch={torch.__version__}')
assert int(transformers.__version__.split('.')[0]) < 5, 'transformers must be < 5'
assert tuple(int(x) for x in numpy.__version__.split('.')[:2]) < (2, 3), 'numpy must be < 2.3'
print('PINS OK')" || fail "dep version check"

# ---- Phase 3: HF login ----
phase_ts 3
echo "--- Phase 3: HF login ---"
huggingface-cli login --token "$HF_TOKEN" --add-to-git-credential 2>&1 | tail -3 || fail "hf login"
abort_check

# ---- Phase 4: serve ----
phase_ts 4
echo "--- Phase 4: infergrid serve --config $CONFIG ---"
nohup bash -c 'while true; do
  nvidia-smi --query-gpu=timestamp,memory.used,memory.total,utilization.gpu,power.draw \
    --format=csv,noheader >> '"$RDIR"'/gpu_trace.csv; sleep 1; done' \
  > "$RDIR/gpu_trace.err" 2>&1 &
echo $! > "$RDIR/gpu_trace.pid"
nohup infergrid serve --config "$CONFIG" --port 8000 --log-level INFO \
  > "$RDIR/server.log" 2>&1 &
echo $! > "$RDIR/server.pid"
SERVER_PID=$(cat "$RDIR/server.pid"); echo "serve pid=$SERVER_PID"

# ---- Phase 5: wait for /v1/models stable ----
phase_ts 5
echo "--- Phase 5: waiting for /v1/models (stable count) ---"
LAST=-1; STABLE=0
for i in $(seq 1 240); do
  abort_check
  kill -0 "$SERVER_PID" 2>/dev/null || fail "serve died. tail: $(tail -120 $RDIR/server.log)"
  grep -q "Failed to pre-load model" "$RDIR/server.log" 2>/dev/null \
    && fail "engine pre-load failure: $(grep -B2 -A6 'Failed to pre-load' $RDIR/server.log | head -80)"
  N=$(curl -sf http://localhost:8000/v1/models 2>/dev/null \
      | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo 0)
  if [ "$N" -gt 0 ] && [ "$N" = "$LAST" ]; then STABLE=$((STABLE+1)); else STABLE=0; fi
  if [ "$STABLE" -ge 1 ]; then echo "READY: $N models stable after $((i*5))s"; break; fi
  LAST=$N; sleep 5
done
[ "$STABLE" -ge 1 ] || fail "models did not stabilize within 20 min (last N=$LAST)"

# ---- Phase 6: smoke (derive model list from /v1/models) ----
phase_ts 6
echo "--- Phase 6: smoke ---"
MODELS=$(curl -sf http://localhost:8000/v1/models | python3 -c 'import sys,json; [print(m["id"]) for m in json.load(sys.stdin)["data"]]')
for M in $MODELS; do
  R=$(curl -s http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" --max-time 60 \
      -d "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"Say one short sentence about GPUs.\"}],\"max_tokens\":32}")
  echo "smoke[$M]=$R" >> "$RDIR/smoke.jsonl"
  C=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('choices',[{}])[0].get('message',{}).get('content',''))" 2>/dev/null || echo "")
  [ -n "$C" ] || fail "smoke empty for $M: $R"
  echo "  $M OK: $C"
done
curl -s http://localhost:8000/infergrid/status > "$RDIR/status_before.json" 2>/dev/null || true

# ---- Phase 7: bench (skipped if --bench-script empty) ----
phase_ts 7
abort_check
# Layer-3: if Phase 4→7 ate more than half the pod budget, the engine
# bring-up went sideways. Don't ALSO burn the bench window on a broken
# setup — fail fast and let trap bundle the diagnostics.
if [ -f "$RDIR/phase_4.ts" ]; then
  P4_START=$(cat "$RDIR/phase_4.ts")
  ENGINE_BRINGUP_S=$(( $(date +%s) - P4_START ))
  HALF_BUDGET=$(( MAX_POD_SECS / 2 ))
  if [ "$ENGINE_BRINGUP_S" -gt "$HALF_BUDGET" ]; then
    fail "engine+smoke took ${ENGINE_BRINGUP_S}s (> half of MAX_POD_SECS=${MAX_POD_SECS}); aborting before bench"
  fi
fi
if [ -n "$BENCH_SCRIPT" ]; then
  echo "--- Phase 7: bench (engine_bringup=${ENGINE_BRINGUP_S:-?}s) ---"
  ARGS="${BENCH_ARGS//RDIR/$RDIR}"
  bash -c "python3 $BENCH_SCRIPT $ARGS 2>&1 | tee $RDIR/bench.log" \
    || echo "WARN: bench non-zero; continuing to capture"
else
  echo "--- Phase 7: SKIPPED (no --bench-script) ---"
fi

# ---- Phase 8: capture ----
phase_ts 8
echo "--- Phase 8: capture ---"
curl -s http://localhost:8000/infergrid/status > "$RDIR/status_after.json"  2>/dev/null || true
curl -s http://localhost:8000/metrics         > "$RDIR/prometheus_dump.txt" 2>/dev/null || true
cp "$CONFIG" "$RDIR/" 2>/dev/null || true
nvidia-smi > "$RDIR/nvidia_smi_final.txt" 2>&1
pip freeze > "$RDIR/pip_freeze.txt" 2>/dev/null || true
git rev-parse HEAD > "$RDIR/git_head.txt" 2>/dev/null || true

STATUS=DONE   # trap will tar + write _DONE marker
exit 0
