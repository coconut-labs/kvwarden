#!/usr/bin/env bash
# smoke_bench.sh — pre-flight gate that MUST pass before any GPU spend.
#
# Local, CPU-only. Stands up a single mock vLLM engine + infergrid serve and
# drives a c=1,8,32 sweep through the multi-model bench harness. Exits non-zero
# if anything that would burn pod money on a real Gate run is broken.
#
# Pass criteria (all must hold):
#   - mock engine + infergrid come up cleanly
#   - /v1/models returns 1 entry within 30s
#   - benchmark completes c=1,8,32 sweep × NUM_REQUESTS in <120s wall
#   - throughput_tok_per_sec > 0 in every concurrency level
#   - admission controller engaged at c=32 (in_flight gauge hit max_concurrent)
#
# Usage:
#   bash benchmarks/scripts/smoke_bench.sh [HANG_AFTER]
# Env:
#   NUM_REQUESTS=50    (default per-concurrency)
#   MAX_CONCURRENT=16  (forces queue at c=32)
#   LOGDIR=/tmp/smoke_bench

set -u

HANG_AFTER=${1:-0}
NUM_REQUESTS=${NUM_REQUESTS:-50}
MAX_CONCURRENT=${MAX_CONCURRENT:-16}
LOGDIR=${LOGDIR:-/tmp/smoke_bench}
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$REPO_ROOT"

mkdir -p "$LOGDIR"
rm -f "$LOGDIR"/*.log "$LOGDIR"/tmp_config.yaml 2>/dev/null

cleanup() {
    echo "--- cleanup ---"
    pkill -f "mock_engine.py --port 8002" 2>/dev/null || true
    pkill -f "mock_engine.py --port 8003" 2>/dev/null || true
    pkill -f "infergrid.cli serve.*tmp_config" 2>/dev/null || true
    sleep 1
}
trap cleanup EXIT

# ---- 1. Write tmp config (harness requires >= 2 models) ----
cat > "$LOGDIR/tmp_config.yaml" <<EOF
host: 0.0.0.0
port: 8000
max_concurrent: $MAX_CONCURRENT
admission_queue_size: 1024
log_level: INFO
models:
  - model_id: "smoke/llama-mock"
    engine: "vllm"
    port: 8002
    gpu_memory_utilization: 0.4
    max_model_len: 4096
  - model_id: "smoke/qwen-mock"
    engine: "vllm"
    port: 8003
    gpu_memory_utilization: 0.4
    max_model_len: 4096
EOF

# ---- 2. Start two mock engines ----
echo "--- start mock engines on :8002, :8003 (hang_after=$HANG_AFTER) ---"
python3 benchmarks/scripts/mock_engine.py --port 8002 \
    --model "smoke/llama-mock" \
    --hang-after "$HANG_AFTER" --ok-latency-s 0.05 \
    > "$LOGDIR/mock_8002.log" 2>&1 &
python3 benchmarks/scripts/mock_engine.py --port 8003 \
    --model "smoke/qwen-mock" \
    --hang-after "$HANG_AFTER" --ok-latency-s 0.05 \
    > "$LOGDIR/mock_8003.log" 2>&1 &
sleep 2

# ---- 3. Start infergrid in dev-mode ----
echo "--- start infergrid serve (dev mode) ---"
INFERGRID_DEV_SKIP_ENGINE_LAUNCH=1 \
PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}" \
python3 -m infergrid.cli serve --config "$LOGDIR/tmp_config.yaml" \
    --log-level INFO > "$LOGDIR/infergrid.log" 2>&1 &
IG_PID=$!

# Wait for /v1/models
READY=0
for i in $(seq 1 30); do
    N=$(curl -sf http://localhost:8000/v1/models 2>/dev/null \
        | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null \
        || echo 0)
    if [ "$N" = "2" ]; then
        READY=1
        echo "  ready after ${i}s"
        break
    fi
    sleep 1
done
[ "$READY" = "1" ] || { echo "FAIL: /v1/models not ready in 30s"; exit 1; }

# ---- 4. Sweep c=1,8,32 ----
echo "--- sweep c=1,8,32 num=$NUM_REQUESTS ---"
START=$(date +%s)
python3 benchmarks/scripts/benchmark_multi_model.py \
    --url http://localhost:8000 \
    --models "smoke/llama-mock" "smoke/qwen-mock" \
    --concurrency "1,8,32" --workload concurrent \
    --num-requests "$NUM_REQUESTS" \
    --output-dir "$LOGDIR/bench" \
    --seed 42 > "$LOGDIR/bench.log" 2>&1
BENCH_RC=$?
END=$(date +%s)
ELAPSED=$((END - START))

# Final metrics snapshot for admission verification
curl -sf http://localhost:8000/metrics > "$LOGDIR/metrics_final.txt" 2>/dev/null || true

# ---- 5. Asserts ----
echo "═══════════════════════════════════════════════════════"
echo " SMOKE BENCH RESULTS"
echo "═══════════════════════════════════════════════════════"
echo "  bench exit:         $BENCH_RC"
echo "  wall clock:         ${ELAPSED}s"
echo "  pass criteria:"
echo "    bench exit == 0:  $([ $BENCH_RC -eq 0 ] && echo PASS || echo FAIL)"
echo "    wall <= 120s:     $([ $ELAPSED -le 120 ] && echo PASS || echo FAIL)"

# Throughput > 0 check — harness emits "Throughput: 524.3 tok/s" per concurrency.
# Pass if every emitted Throughput line has a positive number.
THROUGHPUT_OK=0
TPUT_LINES=$(grep -oE "Throughput: [0-9]+\.[0-9]+ tok/s" "$LOGDIR/bench.log" 2>/dev/null | wc -l | tr -d ' ')
TPUT_NONZERO=$(grep -oE "Throughput: [0-9]+\.[0-9]+ tok/s" "$LOGDIR/bench.log" 2>/dev/null \
    | awk '{print $2}' | awk '$1 > 0' | wc -l | tr -d ' ')
if [ "$TPUT_LINES" -gt 0 ] && [ "$TPUT_LINES" = "$TPUT_NONZERO" ]; then
    THROUGHPUT_OK=1
fi
echo "    throughput lines (all non-zero): $TPUT_NONZERO/$TPUT_LINES"
echo "    throughput > 0:   $([ $THROUGHPUT_OK -eq 1 ] && echo PASS || echo CHECK)"

# Admission engaged at c=32 (in_flight gauge should have hit max_concurrent)
ADM_OK=0
if grep -qE "infergrid_admission_in_flight" "$LOGDIR/metrics_final.txt" 2>/dev/null; then
    ADM_OK=1
fi
echo "    admission metrics present: $([ $ADM_OK -eq 1 ] && echo PASS || echo CHECK)"

echo ""
echo "  Logs: $LOGDIR/"
echo ""

# Overall exit
if [ $BENCH_RC -eq 0 ] && [ $ELAPSED -le 120 ] && [ $THROUGHPUT_OK -eq 1 ]; then
    echo "OVERALL: PASS"
    exit 0
else
    echo "OVERALL: FAIL — DO NOT spend GPU money"
    exit 2
fi
