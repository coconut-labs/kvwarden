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

# ---- Phase 1: apt + clone main ----
echo "--- Phase 1: apt + clone ---"
apt-get update -qq && apt-get install -y -qq git rsync curl jq || fail "apt install"
cd /workspace; rm -rf infergrid
git clone --branch main --depth 1 https://github.com/coconut-labs/infergrid.git || fail "git clone"
cd infergrid; echo "main HEAD: $(git rev-parse --short HEAD)  $(git log -1 --pretty=%s)"

# ---- Phase 2: python deps ----
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
echo "--- Phase 3: HF login ---"
huggingface-cli login --token "$HF_TOKEN" --add-to-git-credential 2>&1 | tail -3 || fail "hf login"

# ---- Phase 4: serve ----
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
echo "--- Phase 5: waiting for /v1/models (stable count) ---"
LAST=-1; STABLE=0
for i in $(seq 1 240); do
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
if [ -n "$BENCH_SCRIPT" ]; then
  echo "--- Phase 7: bench ---"
  ARGS="${BENCH_ARGS//RDIR/$RDIR}"
  bash -c "python3 $BENCH_SCRIPT $ARGS 2>&1 | tee $RDIR/bench.log" \
    || echo "WARN: bench non-zero; continuing to capture"
else
  echo "--- Phase 7: SKIPPED (no --bench-script) ---"
fi

# ---- Phase 8: capture ----
echo "--- Phase 8: capture ---"
curl -s http://localhost:8000/infergrid/status > "$RDIR/status_after.json"  2>/dev/null || true
curl -s http://localhost:8000/metrics         > "$RDIR/prometheus_dump.txt" 2>/dev/null || true
cp "$CONFIG" "$RDIR/" 2>/dev/null || true
nvidia-smi > "$RDIR/nvidia_smi_final.txt" 2>&1
pip freeze > "$RDIR/pip_freeze.txt" 2>/dev/null || true
git rev-parse HEAD > "$RDIR/git_head.txt" 2>/dev/null || true

STATUS=DONE   # trap will tar + write _DONE marker
exit 0
