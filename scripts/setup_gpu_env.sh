#!/usr/bin/env bash
# =============================================================================
# KVWarden — Idempotent GPU Environment Setup
# =============================================================================
# Sets up a fresh Lambda Labs A100 instance for profiling.
# Safe to re-run: every step checks before acting.
#
# Usage:
#   export HF_TOKEN=hf_...
#   bash scripts/setup_gpu_env.sh
#
# Requirements:
#   - NVIDIA GPU with driver installed (Lambda Labs provides this)
#   - Python 3.11+ (Lambda Labs system Python)
#   - HF_TOKEN environment variable for gated model access
# =============================================================================

set -euo pipefail

if [ -z "${VIRTUAL_ENV:-}" ]; then
    log_warn "Not running inside a virtual environment — using system Python."
    log_warn "This is fine for ephemeral cloud instances (RunPod, Lambda Labs)."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default configurations
MODEL_CONFIG="configs/models/llama31_8b.yaml"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --model-config) MODEL_CONFIG="$2"; shift ;;
        *) log_error "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [[ ! -f "$MODEL_CONFIG" ]]; then
    log_warn "Config file not found: $MODEL_CONFIG, continuing without it..."
fi

# Config extraction helper
get_config() {
    python3 -c "import yaml; c=yaml.safe_load(open('$1')); print(c.get('$2', '$3'))"
}

MODEL_ID=$(get_config "$MODEL_CONFIG" "model_id" "meta-llama/Llama-3.1-8B-Instruct")
TENSOR_PARALLEL=$(get_config "$MODEL_CONFIG" "tensor_parallel_size" "1")
MIN_GPUS=$(get_config "$MODEL_CONFIG" "min_gpus" "1")
EST_WEIGHT_GB=$(get_config "$MODEL_CONFIG" "estimated_weight_size_gb" "16")
VLLM_MIN_VERSION=$(get_config "$MODEL_CONFIG" "vllm_min_version" "0.6.0")

VLLM_PORT=8000
SGLANG_PORT=8001

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- Step 0: Preflight checks ----

log_info "=========================================="
log_info "KVWarden GPU Environment Setup"
log_info "=========================================="

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11 ]]; then
    log_error "Python 3.11+ required, found $PYTHON_VERSION"
    exit 1
fi
log_ok "Python $PYTHON_VERSION"

# ---- Step 1: Check CUDA / GPU ----

log_info "Step 1: Checking CUDA & GPU..."

if ! command -v nvidia-smi &>/dev/null; then
    log_error "nvidia-smi not found. No NVIDIA driver installed."
    exit 1
fi

DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)
GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')

log_ok "Driver: $DRIVER_VERSION"
log_ok "GPU: $GPU_NAME ($GPU_MEMORY) x $GPU_COUNT"

if [ "$GPU_COUNT" -lt "$MIN_GPUS" ]; then
    log_error "Model requires $MIN_GPUS GPUs but only $GPU_COUNT found"
    exit 1
fi
log_ok "GPU count satisfies minimum requirement ($MIN_GPUS)"

# Log CUDA version if nvcc is available
if command -v nvcc &>/dev/null; then
    CUDA_VERSION=$(nvcc --version | grep "release" | awk '{print $6}' | tr -d ',')
    log_ok "CUDA: $CUDA_VERSION"
else
    log_warn "nvcc not found — CUDA toolkit not installed (driver-only is fine for inference)"
fi

# ---- Step 2: Install Python dependencies ----

log_info "Step 2: Installing Python dependencies..."

install_if_missing() {
    local package=$1
    local pip_name=${2:-$1}
    if python3 -c "import $package" &>/dev/null; then
        log_ok "$package already installed"
        return 0
    fi
    log_info "Installing $pip_name..."
    pip install --no-cache-dir "$pip_name"
}

# Core project deps
cd "$PROJECT_ROOT"

# Install the project itself with all optional deps
if pip show kvwarden &>/dev/null; then
    log_ok "kvwarden package already installed"
else
    log_info "Installing kvwarden with all dependencies..."
    pip install --no-cache-dir -e ".[dev,profiling]"
fi

# Install inference engines
# vLLM requires specific torch version — install it first to avoid ABI mismatch
if pip show vllm &>/dev/null; then
    log_ok "vLLM already installed"
else
    log_info "Installing vLLM (this may take a few minutes)..."
    pip install --no-cache-dir vllm
    # Ensure torch version matches what vLLM was compiled against
    REQUIRED_TORCH=$(pip show vllm 2>/dev/null | grep "Requires:" | grep -oP 'torch==\S+' | head -1 || true)
    if [[ -n "$REQUIRED_TORCH" ]]; then
        CURRENT_TORCH=$(python3 -c "import torch; print(torch.__version__)" 2>/dev/null || echo "0")
        EXPECTED_TORCH=$(echo "$REQUIRED_TORCH" | sed 's/torch==//')
        if [[ "$CURRENT_TORCH" != "$EXPECTED_TORCH" ]]; then
            log_warn "vLLM needs torch==$EXPECTED_TORCH but found $CURRENT_TORCH — upgrading..."
            pip install --no-cache-dir "torch==$EXPECTED_TORCH" --index-url https://download.pytorch.org/whl/cu124
        fi
    fi
fi

if pip show sglang &>/dev/null; then
    log_ok "SGLang already installed"
else
    log_info "Installing SGLang (this may take a few minutes)..."
    pip install --no-cache-dir "sglang[all]"
fi

# Ensure huggingface-cli is available
if ! command -v huggingface-cli &>/dev/null; then
    pip install --no-cache-dir huggingface_hub[cli]
fi

log_ok "All Python dependencies installed"

log_info "Locking dependency versions to requirements-lock.txt..."
pip freeze > requirements-lock.txt
log_ok "Dependencies locked"

# ---- Step 3: Download model ----

log_info "Step 3: Downloading model ($MODEL_ID)..."

if [[ -z "${HF_TOKEN:-}" ]]; then
    log_error "HF_TOKEN environment variable not set."
    log_error "Get a token at https://huggingface.co/settings/tokens"
    log_error "Then: export HF_TOKEN=hf_..."
    exit 1
fi

# Check available disk space (~2x weight + 50GB buffer)
REQUIRED_DISK=$((EST_WEIGHT_GB * 2 + 50))
AVAILABLE_DISK=$(df -BG /root/.cache 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || df -BG / 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')

if [ "$AVAILABLE_DISK" -lt "$REQUIRED_DISK" ]; then
    log_error "Need ${REQUIRED_DISK}GB disk but only ${AVAILABLE_DISK}GB available"
    exit 1
fi
log_ok "Disk space sufficient (${AVAILABLE_DISK}GB available >= ${REQUIRED_DISK}GB required)"

# Check if model is already cached
MODEL_CACHE_DIR="$HOME/.cache/huggingface/hub/models--$(echo "$MODEL_ID" | tr '/' '--')"
if [[ -d "$MODEL_CACHE_DIR" ]]; then
    log_ok "Model already cached at $MODEL_CACHE_DIR"
else
    log_info "Downloading $MODEL_ID (~${EST_WEIGHT_GB}GB, this takes a while)..."
    # Try 'hf' CLI first (newer), fallback to 'huggingface-cli' (deprecated)
    if command -v hf &>/dev/null; then
        hf download "$MODEL_ID" --token "$HF_TOKEN"
    elif command -v huggingface-cli &>/dev/null; then
        huggingface-cli download "$MODEL_ID" --token "$HF_TOKEN"
    else
        python3 -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL_ID', token='$HF_TOKEN')"
    fi
    log_ok "Model downloaded successfully"
fi

# ---- Step 3.5: Pre-download ShareGPT dataset ----
log_info "Pre-downloading ShareGPT dataset for benchmarks..."
python3 -c "
from datasets import load_dataset
ds = load_dataset('anon8231489123/ShareGPT_Vicuna_unfiltered', split='train')
print(f'ShareGPT dataset cached: {len(ds)} conversations')
" || log_warn "ShareGPT download failed — benchmarks will use synthetic fallback"

# Verify vLLM version
log_info "Verifying vLLM Version >= $VLLM_MIN_VERSION"
VLLM_ACTUAL=$(python3 -c "import vllm; print(vllm.__version__)" 2>/dev/null || echo "0.0.0")
if python3 -c "import packaging.version as v; import sys; sys.exit(0 if v.parse('$VLLM_ACTUAL') >= v.parse('$VLLM_MIN_VERSION') else 1)"; then
    log_ok "vLLM version ($VLLM_ACTUAL) satisfies minimum requirement ($VLLM_MIN_VERSION)"
else
    log_error "vLLM version $VLLM_ACTUAL is too old. Need >= $VLLM_MIN_VERSION"
    exit 1
fi

# ---- Step 4: vLLM smoke test ----

log_info "Step 4: vLLM smoke test..."

# Kill any existing vLLM server
pkill -f "vllm.entrypoints" 2>/dev/null || true
sleep 2

log_info "Starting vLLM server on port $VLLM_PORT (TP=$TENSOR_PARALLEL)..."
python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_ID" \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.8 \
    --tensor-parallel-size "$TENSOR_PARALLEL" \
    --port "$VLLM_PORT" \
    &>/tmp/vllm_setup.log &
VLLM_PID=$!

STARTUP_TIMEOUT=$((TENSOR_PARALLEL * 120 + 60))
# Wait for server to be healthy
log_info "Waiting for vLLM server to load model (up to ${STARTUP_TIMEOUT}s)..."
VLLM_READY=false
for i in $(seq 1 $STARTUP_TIMEOUT); do
    if curl -s "http://localhost:$VLLM_PORT/v1/models" &>/dev/null; then
        VLLM_READY=true
        break
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        log_error "vLLM server process died. Check /tmp/vllm_setup.log"
        cat /tmp/vllm_setup.log | tail -20
        exit 1
    fi
    sleep 1
    if (( i % 30 == 0 )); then
        log_info "  Still waiting... (${i}s elapsed)"
    fi
done

if [[ "$VLLM_READY" != "true" ]]; then
    log_error "vLLM server failed to start within ${STARTUP_TIMEOUT}s"
    kill "$VLLM_PID" 2>/dev/null || true
    exit 1
fi
log_ok "vLLM server is healthy"

# Send smoke test request
log_info "Sending vLLM smoke test request..."
VLLM_RESPONSE=$(curl -s "http://localhost:$VLLM_PORT/v1/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"$MODEL_ID\", \"prompt\": \"Hello, world!\", \"max_tokens\": 16}")

if echo "$VLLM_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('choices')" 2>/dev/null; then
    log_ok "vLLM smoke test passed"
else
    log_error "vLLM smoke test failed. Response: $VLLM_RESPONSE"
    kill "$VLLM_PID" 2>/dev/null || true
    exit 1
fi

# Shut down vLLM
log_info "Shutting down vLLM server..."
kill "$VLLM_PID" 2>/dev/null || true
wait "$VLLM_PID" 2>/dev/null || true
sleep 3

# ---- Step 5: SGLang smoke test ----

log_info "Step 5: SGLang smoke test..."

# Kill any existing SGLang server
pkill -f "sglang" 2>/dev/null || true
sleep 2

log_info "Starting SGLang server on port $SGLANG_PORT (TP=$TENSOR_PARALLEL)..."
python3 -m sglang.launch_server \
    --model-path "$MODEL_ID" \
    --dtype bfloat16 \
    --tp "$TENSOR_PARALLEL" \
    --mem-fraction-static 0.8 \
    --port "$SGLANG_PORT" \
    --host 0.0.0.0 \
    &>/tmp/sglang_setup.log &
SGLANG_PID=$!

STARTUP_TIMEOUT=$((TENSOR_PARALLEL * 120 + 60))
# Wait for server to be healthy
log_info "Waiting for SGLang server to load model (up to ${STARTUP_TIMEOUT}s)..."
SGLANG_READY=false
for i in $(seq 1 $STARTUP_TIMEOUT); do
    if curl -s "http://localhost:$SGLANG_PORT/v1/models" &>/dev/null; then
        SGLANG_READY=true
        break
    fi
    if ! kill -0 "$SGLANG_PID" 2>/dev/null; then
        log_error "SGLang server process died. Check /tmp/sglang_setup.log"
        cat /tmp/sglang_setup.log | tail -20
        exit 1
    fi
    sleep 1
    if (( i % 30 == 0 )); then
        log_info "  Still waiting... (${i}s elapsed)"
    fi
done

if [[ "$SGLANG_READY" != "true" ]]; then
    log_error "SGLang server failed to start within ${STARTUP_TIMEOUT}s"
    kill "$SGLANG_PID" 2>/dev/null || true
    exit 1
fi
log_ok "SGLang server is healthy"

# Send smoke test request
log_info "Sending SGLang smoke test request..."
SGLANG_RESPONSE=$(curl -s "http://localhost:$SGLANG_PORT/v1/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"$MODEL_ID\", \"prompt\": \"Hello, world!\", \"max_tokens\": 16}")

if echo "$SGLANG_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('choices')" 2>/dev/null; then
    log_ok "SGLang smoke test passed"
else
    log_error "SGLang smoke test failed. Response: $SGLANG_RESPONSE"
    kill "$SGLANG_PID" 2>/dev/null || true
    exit 1
fi

# Shut down SGLang
log_info "Shutting down SGLang server..."
kill "$SGLANG_PID" 2>/dev/null || true
wait "$SGLANG_PID" 2>/dev/null || true

# ---- Done ----

log_info "=========================================="
log_ok "Environment setup complete!"
log_info "=========================================="
log_info ""
log_info "Summary:"
log_info "  Python:  $PYTHON_VERSION"
log_info "  Driver:  $DRIVER_VERSION"
log_info "  GPU:     $GPU_NAME ($GPU_MEMORY) x $GPU_COUNT"
log_info "  Model:   $MODEL_ID"
log_info "  vLLM:    $(pip show vllm 2>/dev/null | grep Version | awk '{print $2}')"
log_info "  SGLang:  $(pip show sglang 2>/dev/null | grep Version | awk '{print $2}')"
log_info ""
log_info "Next: bash scripts/run_all_baselines.sh"
