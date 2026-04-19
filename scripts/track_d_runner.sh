#!/bin/bash
# Track D Runner: OOM-under-burst on co-loaded Llama+Qwen.
# D1: gate2_multi_tenant.yaml (InferGrid full stack — admission + budget)
# D2: gate2_round_robin.yaml  (thin proxy — admission off)
#
# Same physical pod, same model load, so model warmup time only paid once.

set -euo pipefail
cd /workspace/infergrid
# HF_TOKEN must be exported in the calling shell — DO NOT bake it in.
: "${HF_TOKEN:?HF_TOKEN env var must be set before invoking this runner}"
export CUDA_VISIBLE_DEVICES=0

wait_gpu_clear() {
    # Cross-arm CUDA-context release on vLLM v1 takes 30-90s after pkill.
    # Without this wait, the next arm's engine init fails because most of
    # the 80GB is still allocated to the prior engine's lingering kernel
    # context.
    local max_wait=180
    for i in $(seq 1 $((max_wait/3))); do
        local mem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
        if [ "$mem" -lt 10000 ]; then
            echo "  GPU clear at $((i*3))s (used=${mem}MiB)"
            return 0
        fi
        if [ $((i % 5)) -eq 0 ]; then
            echo "  Still waiting for GPU release: ${mem}MiB used (${i}*3 = $((i*3))s)"
        fi
        sleep 3
    done
    echo "  GPU still busy after ${max_wait}s — proceeding anyway"
    return 0
}

STAMP=$(date -u +%Y%m%d_%H%M%S)
OUT=/workspace/infergrid/results/gate2_d_$STAMP
mkdir -p $OUT
CHAT="meta-llama/Llama-3.1-8B-Instruct"
RAG="Qwen/Qwen2.5-7B-Instruct"

run_arm() {
    local name=$1
    local cfg=$2
    local d=$OUT/${name}_$STAMP
    mkdir -p $d

    echo ""
    echo "============================================================"
    echo "Track D arm: $name ($cfg)"
    echo "============================================================"

    # NOTE: pkill -f infergrid would self-kill — this script's path
    # contains 'infergrid'. We instead kill the engine workers (which
    # don't share that pattern) by full cmdline + targeted PIDs.
    if pgrep -f "vllm.entrypoints" > /dev/null 2>&1; then
        echo "Killing prior vLLM engine workers..."
        pkill -9 -f "vllm.entrypoints" 2>/dev/null || true
        for pid in $(pgrep -f "infergrid serve" 2>/dev/null); do
            if [ "$pid" != "$$" ] && [ "$pid" != "$BASHPID" ]; then
                kill -9 $pid 2>/dev/null || true
            fi
        done
        echo "Waiting for GPU memory release..."
        wait_gpu_clear
    else
        echo "Fresh pod / no prior workers — skipping cleanup."
    fi

    nohup infergrid serve --config $cfg > $d/server.log 2>&1 &
    local pid=$!
    echo "Server pid=$pid — waiting up to 600s (two models cold-load)..."
    for i in $(seq 1 120); do
        if curl -fs --max-time 3 localhost:8000/health >/dev/null 2>&1; then
            echo "Server ready at ${i}x5s"
            break
        fi
        sleep 5
        [ $i -eq 120 ] && echo "TIMEOUT" && tail -40 $d/server.log && exit 1
    done

    sleep 10  # warmup

    python3 benchmarks/scripts/benchmark_chat_rag_burst.py \
        --url http://localhost:8000 \
        --chat-model $CHAT \
        --rag-model $RAG \
        --chat-rps 5 \
        --burst-rps 15 \
        --burst-dur-s 30 \
        --idle-s 60 \
        --n-bursts 3 \
        --rag-prompt-tokens 4096 \
        --rag-max-tokens 128 \
        --duration-s 300 \
        --timeout-s 120 \
        --output-dir $d 2>&1 | tee $d/bench.log

    curl -s localhost:8000/metrics > $d/prometheus_dump.txt 2>/dev/null || true
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv > $d/nvidia_snapshot.txt 2>/dev/null || true

    kill $pid 2>/dev/null || true
    sleep 3
    pkill -9 -f "vllm.entrypoints" 2>/dev/null || true
    for ipid in $(pgrep -f "infergrid serve" 2>/dev/null); do
        if [ "$ipid" != "$$" ] && [ "$ipid" != "$BASHPID" ]; then
            kill -9 $ipid 2>/dev/null || true
        fi
    done
    sleep 15
}

echo "=== TRACK D ==="
date -u
run_arm "d1_inferGrid"   "configs/gate2_multi_tenant.yaml"
run_arm "d2_roundRobin"  "configs/gate2_round_robin.yaml"

tar czf /workspace/track_d_results_$STAMP.tgz -C $(dirname $OUT) $(basename $OUT)
echo ""
echo "=== TRACK D COMPLETE ==="
date -u
ls -la /workspace/track_d_results_$STAMP.tgz
