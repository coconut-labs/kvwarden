#!/usr/bin/env bash
# run_multi_model_demo.sh — One-command multi-model benchmark for KVWarden
#
# Starts KVWarden serving two models, runs all workload scenarios,
# collects GPU metrics, and packages results.
#
# Works locally (if GPU available) and on cloud (RunPod).
#
# Usage:
#   ./scripts/run_multi_model_demo.sh
#   ./scripts/run_multi_model_demo.sh --url http://already-running:8000
#   GPU_BUDGET=0.90 ./scripts/run_multi_model_demo.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (override with env vars)
# ---------------------------------------------------------------------------

MODEL_A="${MODEL_A:-meta-llama/Llama-3.1-8B-Instruct}"
MODEL_B="${MODEL_B:-Qwen/Qwen2.5-7B-Instruct}"
GPU_BUDGET="${GPU_BUDGET:-0.85}"
KVWARDEN_PORT="${KVWARDEN_PORT:-8000}"
KVWARDEN_URL="${KVWARDEN_URL:-http://localhost:${KVWARDEN_PORT}}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${RESULTS_DIR:-results/multi_model_${TIMESTAMP}}"
CONFIG_FILE="${CONFIG_FILE:-benchmarks/configs/multi_model_scenario.yaml}"
SEED="${SEED:-42}"
SKIP_SERVER="${SKIP_SERVER:-0}"
CONCURRENCY="${CONCURRENCY:-1,8,32}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

cleanup() {
    if [[ "${KVWARDEN_PID:-}" ]]; then
        log "Stopping KVWarden server (PID ${KVWARDEN_PID})..."
        kill "${KVWARDEN_PID}" 2>/dev/null || true
        wait "${KVWARDEN_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

wait_for_server() {
    local url="$1"
    local max_wait="${2:-300}"  # 5 minutes default (model loading is slow)
    local elapsed=0

    log "Waiting for server at ${url} (timeout: ${max_wait}s)..."
    while ! curl -sf "${url}/v1/models" > /dev/null 2>&1; do
        if (( elapsed >= max_wait )); then
            die "Server did not become ready within ${max_wait}s"
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        if (( elapsed % 30 == 0 )); then
            log "  Still waiting... (${elapsed}s elapsed)"
        fi
    done
    log "Server is ready (took ${elapsed}s)"
}

check_gpu() {
    if command -v nvidia-smi &> /dev/null; then
        log "GPU detected:"
        nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
        return 0
    else
        log "WARNING: nvidia-smi not found. GPU metrics will be unavailable."
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

cd "${PROJECT_ROOT}"

log "========================================"
log "KVWarden Multi-Model Demo"
log "========================================"
log "Model A:     ${MODEL_A}"
log "Model B:     ${MODEL_B}"
log "GPU budget:  ${GPU_BUDGET}"
log "Server URL:  ${KVWARDEN_URL}"
log "Results dir: ${RESULTS_DIR}"
log "Config:      ${CONFIG_FILE}"
log ""

# Check GPU availability
check_gpu || true

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Detect environment (local vs RunPod)
if [[ -f /etc/runpod-metadata ]]; then
    log "Running on RunPod"
    echo "runpod" > "${RESULTS_DIR}/environment.txt"
elif [[ -d /proc/driver/nvidia ]]; then
    log "Running locally with NVIDIA GPU"
    echo "local-gpu" > "${RESULTS_DIR}/environment.txt"
else
    log "Running locally without GPU (benchmark may fail on inference)"
    echo "local-no-gpu" > "${RESULTS_DIR}/environment.txt"
fi

# ---------------------------------------------------------------------------
# Step 1: Start KVWarden (unless already running or SKIP_SERVER=1)
# ---------------------------------------------------------------------------

if [[ "${SKIP_SERVER}" == "1" ]] || [[ "${1:-}" == "--url" ]]; then
    if [[ "${1:-}" == "--url" ]]; then
        KVWARDEN_URL="${2:?--url requires a value}"
    fi
    log "Using existing server at ${KVWARDEN_URL}"
else
    log "Starting KVWarden serving two models..."

    # Check if kvwarden CLI is available
    if ! command -v kvwarden &> /dev/null; then
        log "kvwarden CLI not found, trying python -m kvwarden..."
        KVWARDEN_CMD="python -m kvwarden"
    else
        KVWARDEN_CMD="kvwarden"
    fi

    ${KVWARDEN_CMD} serve \
        "${MODEL_A}" "${MODEL_B}" \
        --gpu-budget "${GPU_BUDGET}" \
        --port "${KVWARDEN_PORT}" \
        > "${RESULTS_DIR}/server.log" 2>&1 &
    KVWARDEN_PID=$!
    log "KVWarden started (PID ${KVWARDEN_PID}), log: ${RESULTS_DIR}/server.log"

    # Wait for server to be ready
    wait_for_server "${KVWARDEN_URL}" 300
fi

# ---------------------------------------------------------------------------
# Step 2: Capture pre-benchmark GPU state
# ---------------------------------------------------------------------------

if command -v nvidia-smi &> /dev/null; then
    log "Capturing pre-benchmark GPU state..."
    nvidia-smi --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total,power.draw \
        --format=csv > "${RESULTS_DIR}/gpu_state_before.csv" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Step 3: Run all benchmark scenarios
# ---------------------------------------------------------------------------

log ""
log "========================================"
log "Running multi-model benchmarks"
log "========================================"

BENCHMARK_SCRIPT="${PROJECT_ROOT}/benchmarks/scripts/benchmark_multi_model.py"

if [[ ! -f "${BENCHMARK_SCRIPT}" ]]; then
    die "Benchmark script not found: ${BENCHMARK_SCRIPT}"
fi

python "${BENCHMARK_SCRIPT}" \
    --url "${KVWARDEN_URL}" \
    --config "${CONFIG_FILE}" \
    --models "${MODEL_A}" "${MODEL_B}" \
    --concurrency "${CONCURRENCY}" \
    --output-dir "${RESULTS_DIR}/benchmarks" \
    --seed "${SEED}" \
    --workload all \
    --verbose

BENCH_EXIT=$?

# ---------------------------------------------------------------------------
# Step 4: Capture post-benchmark GPU state
# ---------------------------------------------------------------------------

if command -v nvidia-smi &> /dev/null; then
    log "Capturing post-benchmark GPU state..."
    nvidia-smi --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total,power.draw \
        --format=csv > "${RESULTS_DIR}/gpu_state_after.csv" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Step 5: Package results
# ---------------------------------------------------------------------------

log ""
log "========================================"
log "Packaging results"
log "========================================"

# Copy config file to results for reproducibility
if [[ -f "${CONFIG_FILE}" ]]; then
    cp "${CONFIG_FILE}" "${RESULTS_DIR}/scenario_config.yaml"
fi

# Copy server log if it exists
if [[ -f "${RESULTS_DIR}/server.log" ]]; then
    # Truncate if huge (keep last 1000 lines)
    tail -n 1000 "${RESULTS_DIR}/server.log" > "${RESULTS_DIR}/server_tail.log"
fi

# Create a results tarball
TARBALL="results/multi_model_demo_${TIMESTAMP}.tar.gz"
tar -czf "${TARBALL}" -C "$(dirname "${RESULTS_DIR}")" "$(basename "${RESULTS_DIR}")" 2>/dev/null || true

log ""
log "========================================"
log "Done!"
log "========================================"
log "Results directory: ${RESULTS_DIR}"
if [[ -f "${TARBALL}" ]]; then
    log "Results tarball:   ${TARBALL}"
fi
log ""

# List key output files
log "Key output files:"
find "${RESULTS_DIR}" -name "*.json" -o -name "*.csv" 2>/dev/null | sort | while read -r f; do
    log "  ${f}"
done

exit "${BENCH_EXIT}"
