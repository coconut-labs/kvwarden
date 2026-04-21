#!/usr/bin/env bash
# =============================================================================
# KVWarden — Cloud Benchmark Runner
# =============================================================================
# Designed to run INSIDE a pre-built engine container (vllm/vllm-openai or
# lmsysorg/sglang). Installs only profiling deps, starts the engine, and
# runs the full benchmark suite.
#
# Usage (on RunPod or any GPU cloud):
#   export HF_TOKEN="hf_..."
#   export ENGINE="vllm"   # or "sglang"
#   export GPU_LABEL="a100-sxm"  # for result tagging
#   bash scripts/cloud_benchmark.sh
#
# The engine server (vLLM or SGLang) must either:
#   a) Already be running on the expected port, OR
#   b) This script will start it
# =============================================================================

set -euo pipefail

ENGINE="${ENGINE:-vllm}"
MODEL_ID="${MODEL_ID:-meta-llama/Llama-3.1-8B-Instruct}"
GPU_LABEL="${GPU_LABEL:-unknown-gpu}"
CONCURRENCY="${CONCURRENCY:-1,8,32,64,128,256}"
NUM_REQUESTS="${NUM_REQUESTS:-200}"
REPEATS="${REPEATS:-2}"
SEED="${SEED:-42}"
VLLM_PORT=8000
SGLANG_PORT=30000

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$PROJECT_ROOT/results_${ENGINE}_${GPU_LABEL}_$(date +%Y%m%d_%H%M%S)"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $(date +%H:%M:%S) $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $(date +%H:%M:%S) $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date +%H:%M:%S) $*"; }

# ---------------------------------------------------------------------------
# Step 1: Install profiling dependencies (lightweight, no engine changes)
# ---------------------------------------------------------------------------

log_info "Installing profiling dependencies..."
pip install --no-cache-dir aiohttp numpy pandas pyyaml "nvidia-ml-py>=12.0" datasets matplotlib 2>&1 | tail -3
log_ok "Profiling deps installed"

# ---------------------------------------------------------------------------
# Step 2: HF auth
# ---------------------------------------------------------------------------

if [[ -z "${HF_TOKEN:-}" ]]; then
    log_error "HF_TOKEN not set"
    exit 1
fi

python3 -c "from huggingface_hub import login; login(token='$HF_TOKEN')" 2>/dev/null
log_ok "HF authenticated"

# ---------------------------------------------------------------------------
# Step 3: Download ShareGPT dataset
# ---------------------------------------------------------------------------

log_info "Pre-caching ShareGPT dataset..."
python3 -c "
from datasets import load_dataset
try:
    ds = load_dataset('anon8231489123/ShareGPT_Vicuna_unfiltered', split='train')
    print(f'ShareGPT cached: {len(ds)} conversations')
except Exception as e:
    print(f'ShareGPT download failed ({e}), synthetic fallback will be used')
" 2>&1
log_ok "Dataset ready"

# ---------------------------------------------------------------------------
# Step 4: Start engine if not already running
# ---------------------------------------------------------------------------

ENGINE_PID=""

start_vllm() {
    local port=$VLLM_PORT
    if curl -s "http://localhost:$port/v1/models" &>/dev/null; then
        log_ok "vLLM already running on port $port"
        return 0
    fi
    log_info "Starting vLLM server..."
    python3 -m vllm.entrypoints.openai.api_server \
        --model "$MODEL_ID" \
        --port "$port" \
        --dtype bfloat16 \
        --gpu-memory-utilization 0.85 \
        --no-enable-log-requests \
        &>"$RESULTS_DIR/engine_server.log" &
    ENGINE_PID=$!
    log_info "Waiting for vLLM (up to 300s)..."
    for i in $(seq 1 300); do
        if curl -s "http://localhost:$port/v1/models" &>/dev/null; then
            log_ok "vLLM ready (${i}s)"
            return 0
        fi
        if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
            log_error "vLLM died. See $RESULTS_DIR/engine_server.log"
            tail -20 "$RESULTS_DIR/engine_server.log"
            return 1
        fi
        sleep 1
    done
    log_error "vLLM timeout"
    return 1
}

start_sglang() {
    local port=$SGLANG_PORT
    if curl -s "http://localhost:$port/v1/models" &>/dev/null; then
        log_ok "SGLang already running on port $port"
        return 0
    fi
    log_info "Starting SGLang server..."
    python3 -m sglang.launch_server \
        --model-path "$MODEL_ID" \
        --port "$port" \
        --host 0.0.0.0 \
        --dtype bfloat16 \
        --mem-fraction-static 0.85 \
        &>"$RESULTS_DIR/engine_server.log" &
    ENGINE_PID=$!
    log_info "Waiting for SGLang (up to 300s)..."
    for i in $(seq 1 300); do
        if curl -s "http://localhost:$port/v1/models" &>/dev/null; then
            log_ok "SGLang ready (${i}s)"
            return 0
        fi
        if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
            log_error "SGLang died. See $RESULTS_DIR/engine_server.log"
            tail -20 "$RESULTS_DIR/engine_server.log"
            return 1
        fi
        sleep 1
    done
    log_error "SGLang timeout"
    return 1
}

cleanup() {
    if [[ -n "$ENGINE_PID" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
        log_info "Stopping engine (PID $ENGINE_PID)..."
        kill "$ENGINE_PID" 2>/dev/null || true
        wait "$ENGINE_PID" 2>/dev/null || true
    fi
    # Package results
    if [[ -d "$RESULTS_DIR" ]]; then
        local tarball="${PROJECT_ROOT}/kvwarden_${ENGINE}_${GPU_LABEL}_$(date +%Y%m%d_%H%M%S).tar.gz"
        tar -czf "$tarball" -C "$(dirname "$RESULTS_DIR")" "$(basename "$RESULTS_DIR")" 2>/dev/null || true
        log_info "Results packaged: $tarball"
    fi
}
trap cleanup EXIT

mkdir -p "$RESULTS_DIR"

# Save metadata
cat > "$RESULTS_DIR/run_metadata.json" <<EOF
{
    "engine": "$ENGINE",
    "model": "$MODEL_ID",
    "gpu_label": "$GPU_LABEL",
    "concurrency": "$CONCURRENCY",
    "num_requests": $NUM_REQUESTS,
    "repeats": $REPEATS,
    "seed": $SEED,
    "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
    "gpu": "$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)",
    "driver": "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)",
    "gpu_memory": "$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)"
}
EOF

if [[ "$ENGINE" == "vllm" ]]; then
    start_vllm || exit 1
    BASE_URL="http://localhost:$VLLM_PORT"
    PROFILER="$PROJECT_ROOT/profiling/scripts/profile_vllm_scheduler.py"
elif [[ "$ENGINE" == "sglang" ]]; then
    start_sglang || exit 1
    BASE_URL="http://localhost:$SGLANG_PORT"
    PROFILER="$PROJECT_ROOT/profiling/scripts/profile_sglang_scheduler.py"
else
    log_error "Unknown ENGINE=$ENGINE. Use 'vllm' or 'sglang'."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 5: Run profiling sweep
# ---------------------------------------------------------------------------

log_info "Starting profiling sweep: engine=$ENGINE, gpu=$GPU_LABEL"
log_info "  Concurrency: $CONCURRENCY"
log_info "  Requests: $NUM_REQUESTS per level"
log_info "  Repeats: $REPEATS"

python3 "$PROFILER" \
    --base-url "$BASE_URL" \
    --model "$MODEL_ID" \
    --concurrency "$CONCURRENCY" \
    --num-requests "$NUM_REQUESTS" \
    --repeats "$REPEATS" \
    --workload sharegpt \
    --output-dir "$RESULTS_DIR/profiling" \
    --seed "$SEED"

log_ok "Profiling sweep complete"

# ---------------------------------------------------------------------------
# Step 6: Run head-to-head comparison format (same script, single engine)
# ---------------------------------------------------------------------------

log_info "Running baseline comparison benchmarks (fixed + mixed workloads)..."

VLLM_URL="http://localhost:99999"
SGLANG_URL="http://localhost:99999"
if [[ "$ENGINE" == "vllm" ]]; then
    VLLM_URL="$BASE_URL"
else
    SGLANG_URL="$BASE_URL"
fi

python3 "$PROJECT_ROOT/benchmarks/scripts/run_baseline_comparison.py" \
    --vllm-url "$VLLM_URL" \
    --sglang-url "$SGLANG_URL" \
    --model "$MODEL_ID" \
    --concurrency "$CONCURRENCY" \
    --num-requests "$NUM_REQUESTS" \
    --repeats "$REPEATS" \
    --workload all \
    --output-dir "$RESULTS_DIR/benchmarks" \
    --seed "$SEED"

log_ok "Baseline comparison complete"

# ---------------------------------------------------------------------------
# Step 7: Summary
# ---------------------------------------------------------------------------

python3 "$PROJECT_ROOT/scripts/summarize_results.py" \
    --results-dir "$RESULTS_DIR" \
    --output "$RESULTS_DIR/summary.md" \
    2>/dev/null || log_info "Summary generation skipped"

echo ""
echo -e "${BOLD}${GREEN}=== Benchmark Complete ===${NC}"
echo "  Engine:  $ENGINE"
echo "  GPU:     $GPU_LABEL ($(nvidia-smi --query-gpu=name --format=csv,noheader | head -1))"
echo "  Results: $RESULTS_DIR/"
echo ""
