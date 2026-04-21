#!/usr/bin/env bash
# =============================================================================
# KVWarden — Master Baseline Collection Orchestrator
# =============================================================================
# One command to run all profiling phases and collect baseline data.
# Budget-aware: only one engine loaded at a time (40GB A100 constraint).
# Checkpoint-capable: skip completed phases on re-run.
#
# Usage:
#   bash scripts/run_all_baselines.sh                  # Full run
#   bash scripts/run_all_baselines.sh --dry-run        # Print plan only
#   bash scripts/run_all_baselines.sh --resume         # Resume from checkpoint
#   bash scripts/run_all_baselines.sh --concurrency 1,8,32  # Custom sweep
#
# Phases:
#   1. vLLM profiling (concurrency sweep)
#   2. SGLang profiling (concurrency sweep)
#   3. Head-to-head comparison (all workloads)
#   4. py-spy flame graphs
#   5. Package results into tarball
# =============================================================================

set -euo pipefail

if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "[WARN] Not running inside a virtual environment — using system Python."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Defaults
VLLM_PORT=8000
SGLANG_PORT=8001
CONCURRENCY=""
NUM_REQUESTS=-1
REPEATS=-1
WORKLOAD="sharegpt"
SEED=42
PYSPY_DURATION=60
DRY_RUN=false
RESUME=false
MODEL_CONFIG="configs/models/llama31_8b.yaml"
SKIP_SGLANG=false

# Directories
CHECKPOINT_DIR="$PROJECT_ROOT/.checkpoints"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $(date +%H:%M:%S) $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $(date +%H:%M:%S) $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $(date +%H:%M:%S) $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date +%H:%M:%S) $*"; }
log_phase() { echo -e "\n${BOLD}${BLUE}═══════════════════════════════════════${NC}"; \
              echo -e "${BOLD}${BLUE}  $*${NC}"; \
              echo -e "${BOLD}${BLUE}═══════════════════════════════════════${NC}"; }

# Track PIDs for cleanup
GPU_MON_PID=""
ENGINE_PID=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case $1 in
        --model-config)  MODEL_CONFIG="$2"; shift 2 ;;
        --dry-run)       DRY_RUN=true; shift ;;
        --resume)        RESUME=true; shift ;;
        --concurrency)   CONCURRENCY="$2"; shift 2 ;;
        --num-requests)  NUM_REQUESTS="$2"; shift 2 ;;
        --repeats)       REPEATS="$2"; shift 2 ;;
        --workload)      WORKLOAD="$2"; shift 2 ;;
        --seed)          SEED="$2"; shift 2 ;;
        --pyspy-duration) PYSPY_DURATION="$2"; shift 2 ;;
        --results-dir)   RESULTS_DIR="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--model-config FILE] [--dry-run] [--resume]"
            echo "Options:"
            echo "  --model-config      YAML config file (default: configs/models/llama31_8b.yaml)"
            echo "  --dry-run           Print plan without executing"
            echo "  --resume            Resume from last checkpoint"
            echo "  --concurrency       Comma-separated levels"
            echo "  --num-requests      Requests per level"
            echo "  --repeats           Number of benchmark repeats"
            echo "  --workload          Workload type (default: $WORKLOAD)"
            echo "  --seed              Random seed (default: $SEED)"
            echo "  --pyspy-duration    py-spy recording seconds (default: $PYSPY_DURATION)"
            echo "  --results-dir       Output directory (default: auto-timestamped)"
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ ! -f "$MODEL_CONFIG" ]]; then
    log_warn "Config file not found: $MODEL_CONFIG, proceeding with defaults..."
fi

get_config() {
    python3 -c "import yaml, sys; c=yaml.safe_load(open('$1')); val=c.get('$2', '$3'); print(val if val is not None else '$3')" 2>/dev/null || echo "$3"
}

MODEL_ID=$(get_config "$MODEL_CONFIG" "model_id" "meta-llama/Llama-3.1-8B-Instruct")
SHORT_NAME=$(get_config "$MODEL_CONFIG" "short_name" "unknown")
ARCHITECTURE=$(get_config "$MODEL_CONFIG" "architecture" "dense")
TENSOR_PARALLEL=$(get_config "$MODEL_CONFIG" "tensor_parallel_size" "1")
MAX_MODEL_LEN=$(get_config "$MODEL_CONFIG" "max_model_len" "8192")
GPU_MEM_UTIL=$(get_config "$MODEL_CONFIG" "gpu_memory_utilization" "0.85")
DTYPE=$(get_config "$MODEL_CONFIG" "dtype" "bfloat16")
MEM_FRACTION=$(get_config "$MODEL_CONFIG" "mem_fraction_static" "0.85")
EST_HOURS=$(get_config "$MODEL_CONFIG" "estimated_hours" "8")
EST_COST_HR=$(get_config "$MODEL_CONFIG" "estimated_cost_per_hour" "1.50")

# Pull arrays / defaults if not overridden
if [[ -z "${RESULTS_DIR:-}" ]]; then
    RESULTS_DIR="$PROJECT_ROOT/results_${SHORT_NAME}_$(date +%Y%m%d_%H%M%S)"
fi

if [[ "$CONCURRENCY" == "" ]]; then
    CONCURRENCY=$(python3 -c "import yaml; c=yaml.safe_load(open('$MODEL_CONFIG')); print(','.join(map(str, c.get('concurrency_levels', [1,8,32,64,128,256]))))" 2>/dev/null || echo "1,8,32,64,128,256")
fi

if [[ "$NUM_REQUESTS" == "-1" ]]; then
    NUM_REQUESTS=$(get_config "$MODEL_CONFIG" "num_requests" "200")
fi

if [[ "$REPEATS" == "-1" ]]; then
    REPEATS=$(get_config "$MODEL_CONFIG" "repeats" "3")
fi

VLLM_EXTRA=$(python3 -c "import yaml; c=yaml.safe_load(open('$MODEL_CONFIG')); print(' '.join(c.get('vllm_extra_args', [])))" 2>/dev/null || echo "")

# ---------------------------------------------------------------------------
# Signal handling — save partial results on Ctrl+C
# ---------------------------------------------------------------------------

cleanup() {
    local exit_code=$?
    echo ""
    log_warn "Interrupt received — saving partial results..."

    # Kill background processes
    if [[ -n "$GPU_MON_PID" ]] && kill -0 "$GPU_MON_PID" 2>/dev/null; then
        kill -TERM "$GPU_MON_PID" 2>/dev/null || true
        wait "$GPU_MON_PID" 2>/dev/null || true
        log_info "GPU monitor stopped"
    fi

    if [[ -n "$ENGINE_PID" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
        kill "$ENGINE_PID" 2>/dev/null || true
        wait "$ENGINE_PID" 2>/dev/null || true
        log_info "Engine server stopped"
    fi

    # Package whatever we have
    if [[ -d "$RESULTS_DIR" ]] && [[ "$(ls -A "$RESULTS_DIR" 2>/dev/null)" ]]; then
        local tarball="${RESULTS_DIR}.partial.tar.gz"
        tar -czf "$tarball" -C "$(dirname "$RESULTS_DIR")" "$(basename "$RESULTS_DIR")" 2>/dev/null || true
        log_info "Partial results saved to $tarball"
    fi

    exit "$exit_code"
}

trap cleanup SIGINT SIGTERM

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

phase_done() {
    local phase=$1
    [[ -f "$CHECKPOINT_DIR/phase_${phase}_done" ]]
}

mark_phase_done() {
    local phase=$1
    mkdir -p "$CHECKPOINT_DIR"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$CHECKPOINT_DIR/phase_${phase}_done"
    log_ok "Phase $phase checkpoint saved"
}

estimate_time() {
    local phase=$1
    case $phase in
        1) echo "~15-25 min (vLLM startup + concurrency sweep)" ;;
        2) echo "~15-25 min (SGLang startup + concurrency sweep)" ;;
        3) echo "~30-40 min (both engines, all workloads)" ;;
        4) echo "~5-10 min (py-spy flame graphs)" ;;
        5) echo "<1 min (packaging)" ;;
    esac
}

check_gpu() {
    if ! command -v nvidia-smi &>/dev/null; then
        log_error "No GPU detected (nvidia-smi not found)"
        log_info "This script requires an NVIDIA GPU. Run setup_gpu_env.sh first."
        exit 1
    fi
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
}

start_vllm() {
    # Kill any stale server on our port from a prior run
    if lsof -ti:$VLLM_PORT &>/dev/null; then
        log_warn "Found existing process on port $VLLM_PORT — killing it"
        lsof -ti:$VLLM_PORT | xargs kill -9 2>/dev/null || true
        sleep 3
    fi

    log_info "Starting vLLM server on port $VLLM_PORT..."
    python3 -m vllm.entrypoints.openai.api_server \
        --model "$MODEL_ID" \
        --port "$VLLM_PORT" \
        --tensor-parallel-size "$TENSOR_PARALLEL" \
        --dtype "$DTYPE" \
        --gpu-memory-utilization "$GPU_MEM_UTIL" \
        --max-model-len "$MAX_MODEL_LEN" \
        --no-enable-log-requests \
        $VLLM_EXTRA \
        &>"$RESULTS_DIR/vllm_server.log" &
    ENGINE_PID=$!

    STARTUP_TIMEOUT=$((TENSOR_PARALLEL * 120 + 120))
    log_info "Waiting for vLLM to load model (up to ${STARTUP_TIMEOUT}s)..."
    for i in $(seq 1 $STARTUP_TIMEOUT); do
        if curl -s "http://localhost:$VLLM_PORT/v1/models" &>/dev/null; then
            # Verify OUR server is the one responding (not a zombie from a prior run)
            RESPONDING_PID=$(lsof -ti:$VLLM_PORT 2>/dev/null | head -1)
            if [[ -n "$RESPONDING_PID" ]] && [[ "$RESPONDING_PID" != "$ENGINE_PID" ]]; then
                log_error "Port $VLLM_PORT responded but PID $RESPONDING_PID != launched PID $ENGINE_PID — stale server detected"
                return 1
            fi
            log_ok "vLLM server ready (${i}s)"
            return 0
        fi
        if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
            log_error "vLLM server died. See $RESULTS_DIR/vllm_server.log"
            return 1
        fi
        sleep 1
    done
    log_error "vLLM server timeout (${STARTUP_TIMEOUT}s)"
    return 1
}

start_sglang() {
    if [ "$SKIP_SGLANG" = "true" ]; then
        log_warn "SGLang is skipped for this model."
        return 1
    fi
    
    if [ "$ARCHITECTURE" = "moe" ] && [ -z "${SGLANG_MOE_SUPPORTED:-}" ]; then
        log_info "Checking SGLang MoE support..."
        SGLANG_SUPPORTED=true
        timeout 180 python3 -m sglang.launch_server \
            --model-path "$MODEL_ID" --tp "$TENSOR_PARALLEL" --port 19999 \
            --dtype "$DTYPE" --max-total-tokens 4096 2>&1 | tail -5 || SGLANG_SUPPORTED=false
        kill %1 2>/dev/null || true
        sleep 10
        
        if [ "$SGLANG_SUPPORTED" = false ]; then
            log_warn "SGLang does not support $MODEL_ID — skipping SGLang profiling"
            log_info "This is expected for some MoE models. vLLM-only results will be collected."
            SKIP_SGLANG=true
            export SGLANG_MOE_SUPPORTED=false
            return 1
        else
            export SGLANG_MOE_SUPPORTED=true
        fi
    fi

    # Kill any stale server on our port from a prior run
    if lsof -ti:$SGLANG_PORT &>/dev/null; then
        log_warn "Found existing process on port $SGLANG_PORT — killing it"
        lsof -ti:$SGLANG_PORT | xargs kill -9 2>/dev/null || true
        sleep 3
    fi

    log_info "Starting SGLang server on port $SGLANG_PORT..."
    python3 -m sglang.launch_server \
        --model-path "$MODEL_ID" \
        --dtype "$DTYPE" \
        --tp "$TENSOR_PARALLEL" \
        --mem-fraction-static "$MEM_FRACTION" \
        --max-total-tokens "$MAX_MODEL_LEN" \
        --port "$SGLANG_PORT" \
        --host 0.0.0.0 \
        &>"$RESULTS_DIR/sglang_server.log" &
    ENGINE_PID=$!

    STARTUP_TIMEOUT=$((TENSOR_PARALLEL * 120 + 120))
    log_info "Waiting for SGLang to load model (up to ${STARTUP_TIMEOUT}s)..."
    for i in $(seq 1 $STARTUP_TIMEOUT); do
        if curl -s "http://localhost:$SGLANG_PORT/v1/models" &>/dev/null; then
            # Verify OUR server is the one responding (not a zombie from a prior run)
            RESPONDING_PID=$(lsof -ti:$SGLANG_PORT 2>/dev/null | head -1)
            if [[ -n "$RESPONDING_PID" ]] && [[ "$RESPONDING_PID" != "$ENGINE_PID" ]]; then
                log_error "Port $SGLANG_PORT responded but PID $RESPONDING_PID != launched PID $ENGINE_PID — stale server detected"
                return 1
            fi
            log_ok "SGLang server ready (${i}s)"
            return 0
        fi
        if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
            log_error "SGLang server died. See $RESULTS_DIR/sglang_server.log"
            return 1
        fi
        sleep 1
    done
    log_error "SGLang server timeout (${STARTUP_TIMEOUT}s)"
    return 1
}

stop_engine() {
    if [[ -n "$ENGINE_PID" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
        log_info "Stopping engine (PID $ENGINE_PID)..."
        kill "$ENGINE_PID" 2>/dev/null || true
        wait "$ENGINE_PID" 2>/dev/null || true
        ENGINE_PID=""
        sleep 5  # Let GPU memory fully release
    fi
}

start_gpu_monitor() {
    local label=$1
    local output="$RESULTS_DIR/gpu_metrics_${label}.csv"
    python3 "$SCRIPT_DIR/gpu_monitor.py" \
        --output "$output" \
        --interval-ms 500 \
        &>/dev/null &
    GPU_MON_PID=$!
    log_info "GPU monitor started (PID $GPU_MON_PID) → $output"
}

stop_gpu_monitor() {
    if [[ -n "$GPU_MON_PID" ]] && kill -0 "$GPU_MON_PID" 2>/dev/null; then
        kill -TERM "$GPU_MON_PID" 2>/dev/null || true
        wait "$GPU_MON_PID" 2>/dev/null || true
        GPU_MON_PID=""
        log_info "GPU monitor stopped"
    fi
}

# ---------------------------------------------------------------------------
# Print plan
# ---------------------------------------------------------------------------

print_plan() {
    echo ""
    echo -e "${BOLD}KVWarden Baseline Collection Plan${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Model:        $MODEL_ID"
    echo "  Concurrency:  $CONCURRENCY"
    echo "  Requests:     $NUM_REQUESTS per level"
    echo "  Workload:     $WORKLOAD"
    echo "  Seed:         $SEED"
    echo "  Results:      $RESULTS_DIR"
    echo ""
    TOTAL_COST=$(python3 -c "print(f'${EST_HOURS} * ${EST_COST_HR} = \${${EST_HOURS} * ${EST_COST_HR}:.2f}')")
    echo "  Phase 1: vLLM profiling          $(estimate_time 1)"
    echo "  Phase 2: SGLang profiling         $(estimate_time 2)"
    echo "  Phase 3: Head-to-head comparison  $(estimate_time 3)"
    echo "  Phase 4: py-spy flame graphs      $(estimate_time 4)"
    echo "  Phase 5: Package results          $(estimate_time 5)"
    echo ""
    echo "  Estimated total: ~${EST_HOURS} hours"
    echo "  Estimated cost:  ~${TOTAL_COST}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

print_plan

if [[ "$DRY_RUN" == "true" ]]; then
    log_info "Dry run mode — no actions taken."
    exit 0
fi

# Preflight
check_gpu

# ---------------------------------------------------------------------------
# Preflight: verify all profiling dependencies are importable
# ---------------------------------------------------------------------------

log_info "Checking profiling dependencies..."

MISSING_DEPS=()

python3 -c "import pandas" 2>/dev/null || MISSING_DEPS+=("pandas")
python3 -c "import numpy" 2>/dev/null || MISSING_DEPS+=("numpy")
python3 -c "import aiohttp" 2>/dev/null || MISSING_DEPS+=("aiohttp")
python3 -c "import yaml" 2>/dev/null || MISSING_DEPS+=("pyyaml")
python3 -c "import datasets" 2>/dev/null || MISSING_DEPS+=("datasets")
python3 -c "import pynvml; pynvml.nvmlInit(); pynvml.nvmlShutdown()" 2>/dev/null || MISSING_DEPS+=("nvidia-ml-py (pynvml)")
python3 -c "import matplotlib" 2>/dev/null || MISSING_DEPS+=("matplotlib")

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    log_error "Missing dependencies: ${MISSING_DEPS[*]}"
    log_error "Run: pip install pandas numpy aiohttp pyyaml datasets nvidia-ml-py matplotlib"
    log_error "Or re-run: source scripts/setup_venv.sh"
    exit 1
fi

log_ok "All profiling dependencies verified"

mkdir -p "$RESULTS_DIR"

if [[ -f "$MODEL_CONFIG" ]]; then
    cp "$MODEL_CONFIG" "$RESULTS_DIR/model_config.yaml"
fi

# Save run metadata
cat > "$RESULTS_DIR/run_metadata.json" <<EOF
{
    "model": "$MODEL_ID",
    "concurrency": "$CONCURRENCY",
    "num_requests": $NUM_REQUESTS,
    "workload": "$WORKLOAD",
    "seed": $SEED,
    "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
    "gpu": "$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)",
    "driver": "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
}
EOF

START_TIME=$SECONDS

# ======================== Phase 1: vLLM Profiling ========================

if phase_done 1 && [[ "$RESUME" == "true" ]]; then
    log_info "Phase 1 already complete — skipping"
else
    log_phase "Phase 1/5: vLLM Profiling ($(estimate_time 1))"

    start_vllm || exit 1
    start_gpu_monitor "phase1_vllm"

    python3 "$PROJECT_ROOT/profiling/scripts/profile_vllm_scheduler.py" \
        --base-url "http://localhost:$VLLM_PORT" \
        --model "$MODEL_ID" \
        --concurrency "$CONCURRENCY" \
        --num-requests "$NUM_REQUESTS" \
        --repeats "$REPEATS" \
        --workload "$WORKLOAD" \
        --output-dir "$RESULTS_DIR/profiling/vllm" \
        --seed "$SEED"

    stop_gpu_monitor
    stop_engine
    mark_phase_done 1
fi

PHASE1_TIME=$((SECONDS - START_TIME))
log_info "Phase 1 elapsed: $((PHASE1_TIME / 60))m $((PHASE1_TIME % 60))s"

# ======================== Phase 2: SGLang Profiling ========================

if phase_done 2 && [[ "$RESUME" == "true" ]]; then
    log_info "Phase 2 already complete — skipping"
else
    log_phase "Phase 2/5: SGLang Profiling ($(estimate_time 2))"

    if start_sglang; then
        start_gpu_monitor "phase2_sglang"

        python3 "$PROJECT_ROOT/profiling/scripts/profile_sglang_scheduler.py" \
            --base-url "http://localhost:$SGLANG_PORT" \
            --model "$MODEL_ID" \
            --concurrency "$CONCURRENCY" \
            --num-requests "$NUM_REQUESTS" \
            --repeats "$REPEATS" \
            --workload "$WORKLOAD" \
            --output-dir "$RESULTS_DIR/profiling/sglang" \
            --seed "$SEED"

        stop_gpu_monitor
        stop_engine
    else
        log_warn "SGLang unavailable — Phase 2 skipped. vLLM-only results will be collected."
        echo "sglang_skipped=true" >> "$RESULTS_DIR/run_metadata.json"
    fi
    mark_phase_done 2
fi

PHASE2_TIME=$((SECONDS - START_TIME - PHASE1_TIME))
log_info "Phase 2 elapsed: $((PHASE2_TIME / 60))m $((PHASE2_TIME % 60))s"

# ======================== Phase 3: Head-to-Head ========================
#
# Single-GPU strategy: run each engine individually with identical workloads
# (same seed), save results to the same output dir, then compute comparison.
# run_baseline_comparison.py gracefully skips unreachable engines.
#

if phase_done 3 && [[ "$RESUME" == "true" ]]; then
    log_info "Phase 3 already complete — skipping"
else
    log_phase "Phase 3/5: Head-to-Head Comparison ($(estimate_time 3))"

    H2H_DIR="$RESULTS_DIR/benchmarks/baseline"
    mkdir -p "$H2H_DIR"

    # Run vLLM with all workloads
    start_vllm || exit 1
    start_gpu_monitor "phase3_h2h_vllm"

    python3 "$PROJECT_ROOT/benchmarks/scripts/run_baseline_comparison.py" \
        --vllm-url "http://localhost:$VLLM_PORT" \
        --sglang-url "http://localhost:99999" \
        --model "$MODEL_ID" \
        --concurrency "$CONCURRENCY" \
        --num-requests "$NUM_REQUESTS" \
        --repeats "$REPEATS" \
        --workload all \
        --output-dir "$H2H_DIR" \
        --seed "$SEED"

    stop_gpu_monitor
    stop_engine

    # Run SGLang with identical workloads (same seed → same requests)
    if [[ "$SKIP_SGLANG" != "true" ]] && start_sglang; then
        start_gpu_monitor "phase3_h2h_sglang"

        python3 "$PROJECT_ROOT/benchmarks/scripts/run_baseline_comparison.py" \
            --vllm-url "http://localhost:99999" \
            --sglang-url "http://localhost:$SGLANG_PORT" \
            --model "$MODEL_ID" \
            --concurrency "$CONCURRENCY" \
            --num-requests "$NUM_REQUESTS" \
            --repeats "$REPEATS" \
            --workload all \
            --output-dir "$H2H_DIR" \
            --seed "$SEED"

        stop_gpu_monitor
        stop_engine
    else
        log_warn "SGLang unavailable — Phase 3 head-to-head will be vLLM-only."
    fi

    # Both runs wrote CSVs to the same dir (vllm_c32.csv, sglang_c32.csv etc)
    # The second run's comparison_summary.json will have both engine data
    log_ok "Head-to-head data collected in $H2H_DIR"

    mark_phase_done 3
fi

PHASE3_TIME=$((SECONDS - START_TIME - PHASE1_TIME - PHASE2_TIME))
log_info "Phase 3 elapsed: $((PHASE3_TIME / 60))m $((PHASE3_TIME % 60))s"

# ======================== Phase 4: py-spy Flame Graphs ========================

if phase_done 4 && [[ "$RESUME" == "true" ]]; then
    log_info "Phase 4 already complete — skipping"
else
    log_phase "Phase 4/5: py-spy Flame Graphs ($(estimate_time 4))"

    if ! command -v py-spy &>/dev/null; then
        log_warn "py-spy not installed — skipping flame graph generation"
        log_info "Install with: pip install py-spy"
    else
        # vLLM flame graph
        start_vllm || exit 1

        python3 "$PROJECT_ROOT/profiling/scripts/profile_vllm_scheduler.py" \
            --base-url "http://localhost:$VLLM_PORT" \
            --model "$MODEL_ID" \
            --concurrency "32" \
            --num-requests 100 \
            --workload "$WORKLOAD" \
            --output-dir "$RESULTS_DIR/profiling/vllm" \
            --profile-internal \
            --duration "$PYSPY_DURATION" \
            --seed "$SEED"

        stop_engine

        # SGLang flame graph (skip if unavailable)
        if [[ "$SKIP_SGLANG" != "true" ]] && start_sglang; then
            python3 "$PROJECT_ROOT/profiling/scripts/profile_sglang_scheduler.py" \
                --base-url "http://localhost:$SGLANG_PORT" \
                --model "$MODEL_ID" \
                --concurrency "32" \
                --num-requests 100 \
                --workload "$WORKLOAD" \
                --output-dir "$RESULTS_DIR/profiling/sglang" \
                --profile-internal \
                --duration "$PYSPY_DURATION" \
                --seed "$SEED"

            stop_engine
        else
            log_warn "SGLang unavailable — skipping SGLang flame graphs."
        fi
    fi

    mark_phase_done 4
fi

# ======================== Phase 5: Package Results ========================

log_phase "Phase 5/5: Packaging Results"

# Generate summary
log_info "Generating results summary..."
python3 "$SCRIPT_DIR/summarize_results.py" \
    --results-dir "$RESULTS_DIR" \
    --output "$RESULTS_DIR/summary.md" \
    2>/dev/null || log_warn "Summary generation failed (non-critical)"

# Create tarball
TARBALL="kvwarden_results_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$PROJECT_ROOT/$TARBALL" -C "$(dirname "$RESULTS_DIR")" "$(basename "$RESULTS_DIR")"
log_ok "Results packaged: $TARBALL"

# Clean up checkpoints
rm -rf "$CHECKPOINT_DIR"

# ======================== Final Summary ========================

TOTAL_TIME=$((SECONDS - START_TIME))

log_phase "Baseline Collection Complete"
echo ""
echo -e "  Total time: ${BOLD}$((TOTAL_TIME / 60))m $((TOTAL_TIME % 60))s${NC}"
echo "  Results:    $RESULTS_DIR/"
echo "  Tarball:    $TARBALL"
echo ""
echo "  Phase 1 (vLLM):   $((PHASE1_TIME / 60))m $((PHASE1_TIME % 60))s"
echo "  Phase 2 (SGLang):  $((PHASE2_TIME / 60))m $((PHASE2_TIME % 60))s"
echo "  Phase 3 (H2H):     $((PHASE3_TIME / 60))m $((PHASE3_TIME % 60))s"
echo ""
echo "  Estimated cost: \$$(echo "scale=2; $TOTAL_TIME / 3600 * 1.29" | bc 2>/dev/null || echo "N/A")"
echo ""
log_info "Next steps:"
log_info "  1. Download tarball: scp user@host:$PROJECT_ROOT/$TARBALL ."
log_info "  2. Run summary:     python scripts/summarize_results.py --results-dir $RESULTS_DIR"
log_info "  3. Update findings:  Review docs/phase1_findings.md"
