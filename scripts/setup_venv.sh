#!/bin/bash
# setup_venv.sh — Create isolated Python environment for KVWarden GPU runs
# Usage: source scripts/setup_venv.sh
# MUST be sourced (not executed) so the venv stays active in your shell

set -euo pipefail

VENV_DIR="${KVWARDEN_VENV:-/workspace/venv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "[INFO] ============================================"
echo "[INFO] KVWarden Virtual Environment Setup"
echo "[INFO] ============================================"

# Step 1: Create venv with --without-pip first, then bootstrap pip
# This ensures we don't inherit ANY system packages
echo "[INFO] Creating clean virtual environment at $VENV_DIR..."
if [ -d "$VENV_DIR" ]; then
    echo "[WARN] $VENV_DIR already exists. Reusing."
else
    python3 -m venv "$VENV_DIR" --clear
fi

# Step 2: Activate
echo "[INFO] Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# Step 3: Upgrade pip inside venv
echo "[INFO] Upgrading pip..."
pip install --upgrade pip setuptools wheel

# Step 4: Install vLLM FIRST (it pulls correct torch + transformers + tokenizers)
echo "[INFO] Installing vLLM (this pulls torch, transformers, tokenizers)..."
echo "[INFO] This may take 5-10 minutes..."
pip install vllm

# Step 5: Verify the critical packages vLLM pulled
echo "[INFO] Verifying vLLM dependency chain..."
TORCH_VER=$(python3 -c "import torch; print(torch.__version__)")
TRANSFORMERS_VER=$(python3 -c "import transformers; print(transformers.__version__)")
TOKENIZERS_VER=$(python3 -c "import tokenizers; print(tokenizers.__version__)")
echo "[OK]    torch=$TORCH_VER"
echo "[OK]    transformers=$TRANSFORMERS_VER"  
echo "[OK]    tokenizers=$TOKENIZERS_VER"

# Step 6: Install profiling dependencies (these don't conflict with anything)
echo "[INFO] Installing profiling dependencies..."
pip install aiohttp numpy pandas matplotlib pyyaml "nvidia-ml-py>=12.0" datasets pytest pytest-asyncio

# Step 7: Install kvwarden package in dev mode
echo "[INFO] Installing kvwarden package..."
cd "$PROJECT_ROOT"
pip install -e ".[dev]" --no-deps 2>/dev/null || pip install -e . --no-deps 2>/dev/null || echo "[WARN] Could not install kvwarden package — profiling scripts will still work"

# Step 8: Lock versions
echo "[INFO] Locking all installed versions..."
pip freeze > "$PROJECT_ROOT/requirements-lock.txt"
echo "[OK]    Locked to $PROJECT_ROOT/requirements-lock.txt"

# Step 9: Quick smoke test
echo "[INFO] Running smoke tests..."
python3 -c "import vllm; print(f'vLLM {vllm.__version__} OK')"
python3 -c "import torch; print(f'PyTorch {torch.__version__}, CUDA available: {torch.cuda.is_available()}')"
python3 -c "import transformers; print(f'Transformers {transformers.__version__} OK')"
python3 -c "
from transformers import AutoTokenizer
t = AutoTokenizer.from_pretrained('meta-llama/Llama-3.1-8B-Instruct', token='$HF_TOKEN')
print(f'Tokenizer loaded OK: {type(t).__name__}')
" 2>/dev/null || echo "[WARN] Tokenizer test skipped (HF_TOKEN not set or model not cached)"

# Step 9b: Verify ALL profiling dependencies are importable
echo "[INFO] Verifying all profiling dependencies..."
python3 -c "
import sys
failures = []
for mod_name, pip_name in [
    ('pandas', 'pandas'),
    ('numpy', 'numpy'),
    ('aiohttp', 'aiohttp'),
    ('yaml', 'pyyaml'),
    ('datasets', 'datasets'),
    ('pynvml', 'nvidia-ml-py'),
    ('matplotlib', 'matplotlib'),
    ('pytest', 'pytest'),
]:
    try:
        __import__(mod_name)
    except ImportError:
        failures.append(f'{mod_name} (pip install {pip_name})')

if failures:
    print(f'[ERROR] Missing packages after install:')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('[OK]    All profiling dependencies importable')
"

if [[ $? -ne 0 ]]; then
    echo "[ERROR] Dependency verification failed. Your venv may have conflicts."
    echo "[ERROR] Try: pip install pandas numpy aiohttp pyyaml datasets nvidia-ml-py matplotlib --force-reinstall"
    exit 1
fi

echo ""
echo "[OK]    ============================================"
echo "[OK]    Virtual environment ready!"
echo "[OK]    Location: $VENV_DIR"
echo "[OK]    To reactivate: source $VENV_DIR/bin/activate"
echo "[OK]    ============================================"
echo ""
echo "[INFO] Next steps:"
echo "  export HF_TOKEN='your_token'"
echo "  bash scripts/setup_gpu_env.sh --model-config configs/models/llama4_scout.yaml"
echo "  nohup bash scripts/run_all_baselines.sh --model-config configs/models/llama4_scout.yaml --repeats 2 > run.log 2>&1 &"
