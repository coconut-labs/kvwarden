#!/usr/bin/env bash
# Gate 0.5 reproducer — recreates the 2026-04-18 bench harness stall locally.
#
# Runs 2 mock engines (ports 8002, 8003) + kvwarden serve (port 8000)
# + the multi-model benchmark at concurrency=1 alternating. With
# --hang-after 20 the engines start stalling on request 21, mimicking
# the real incident.
#
# Pass/fail:
#   BEFORE resilience fixes: script hangs ~300s per stalled request.
#   AFTER resilience fixes:  fast failure, script completes in <120s.
#
# Usage:
#   ./benchmarks/scripts/repro_gate0_stall.sh [HANG_AFTER=20]

set -euo pipefail

HANG_AFTER=${1:-20}
NUM_REQUESTS=${NUM_REQUESTS:-50}
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$REPO_ROOT"

LOGDIR=${LOGDIR:-/tmp/gate05_repro}
mkdir -p "$LOGDIR"
rm -f "$LOGDIR"/*.log

cleanup() {
    echo "--- cleanup ---"
    jobs -p | xargs -r kill 2>/dev/null || true
    pkill -f "mock_engine.py --port 8002" 2>/dev/null || true
    pkill -f "mock_engine.py --port 8003" 2>/dev/null || true
    pkill -f "kvwarden serve --port 8000" 2>/dev/null || true
}
trap cleanup EXIT

echo "--- starting mock engines (hang_after=$HANG_AFTER) ---"
python3 benchmarks/scripts/mock_engine.py --port 8002 \
    --model "meta-llama/Llama-3.1-8B-Instruct" \
    --hang-after "$HANG_AFTER" --ok-latency-s 0.2 \
    > "$LOGDIR/mock_8002.log" 2>&1 &
python3 benchmarks/scripts/mock_engine.py --port 8003 \
    --model "Qwen/Qwen2.5-7B-Instruct" \
    --hang-after "$HANG_AFTER" --ok-latency-s 0.2 \
    > "$LOGDIR/mock_8003.log" 2>&1 &

sleep 2

echo "--- starting kvwarden serve (dev mode: skip engine launch, attach to mocks) ---"
# Use gate05_mock.yaml so engines are pinned to ports 8002/8003 matching mocks.
# Without --config the router auto-allocates from 8001 and the mapping breaks.
KVWARDEN_ENGINE_LOG_DIR="$LOGDIR" \
KVWARDEN_DEV_SKIP_ENGINE_LAUNCH=1 \
PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}" \
python3 -m kvwarden.cli serve \
    --config benchmarks/configs/gate05_mock.yaml \
    --log-level INFO \
    > "$LOGDIR/kvwarden.log" 2>&1 &
IG_PID=$!

# Wait for /v1/models
for i in $(seq 1 30); do
    N=$(curl -sf http://localhost:8000/v1/models 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null \
        || echo 0)
    if [ "$N" = "2" ]; then
        echo "  ready after ${i}s"
        break
    fi
    sleep 1
done

echo "--- running benchmark (c=1 alternating, num=$NUM_REQUESTS) ---"
START=$(date +%s)
python3 benchmarks/scripts/benchmark_multi_model.py \
    --url http://localhost:8000 \
    --models meta-llama/Llama-3.1-8B-Instruct Qwen/Qwen2.5-7B-Instruct \
    --concurrency 1 --workload alternating --num-requests "$NUM_REQUESTS" \
    --output-dir "$LOGDIR/bench" \
    --seed 42 \
    ${BENCH_EXTRA:-} \
    > "$LOGDIR/bench.log" 2>&1 || true
END=$(date +%s)
ELAPSED=$((END - START))

echo ""
echo "═══════════════════════════════════════════════════════════"
echo " REPRO RESULT: elapsed=${ELAPSED}s (hang_after=$HANG_AFTER, num=$NUM_REQUESTS)"
echo "═══════════════════════════════════════════════════════════"
echo "  BEFORE fixes: expected >= 300s (one stall × 300s timeout)"
echo "  AFTER fixes:  expected <= 120s (stall short-circuited)"
echo ""
echo "Logs: $LOGDIR/"
tail -3 "$LOGDIR/bench.log" || true
