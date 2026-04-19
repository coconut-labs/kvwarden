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

    pkill -9 -f infergrid 2>/dev/null || true
    pkill -9 -f vllm 2>/dev/null || true
    sleep 5

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
    pkill -9 -f infergrid 2>/dev/null || true
    pkill -9 -f vllm 2>/dev/null || true
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
