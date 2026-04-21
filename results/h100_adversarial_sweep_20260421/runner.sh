#!/bin/bash
# Orchestrator for H100 adversarial sweep 20260421.
# Runs on pod. Manages infergrid server + vllm backend + bench loops.
# Emits heartbeats to keep the stream watchdog alive.
set -u

REPO=/workspace/infergrid
RDIR=/workspace/results/h100_adversarial_sweep_20260421
cd "$REPO"
source /root/.gate_env
export HF_TOKEN
export VLLM_USE_V1=0
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

mkdir -p "$RDIR"/{overhead,rps_sweep,n_sweep,fifo_anchor,server_logs,configs}
cp configs/gate21_fairness_n8.yaml "$RDIR/configs/"
cp configs/gate22_fifo_n8.yaml "$RDIR/configs/"
git rev-parse HEAD > "$RDIR/git_head.txt"
pip show vllm transformers numpy 2>&1 | grep -E "^(Name|Version)" > "$RDIR/pip_relevant.txt"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader > "$RDIR/gpu_info.txt"

hb() { echo "HB $(date -u +%H:%M:%S) $*"; }

wait_for_health() {
  local port=$1
  local timeout=$2
  local start=$(date +%s)
  while true; do
    if curl -sf "http://localhost:$port/v1/models" >/dev/null 2>&1; then
      hb "server on port=$port HEALTHY"
      return 0
    fi
    local elapsed=$(( $(date +%s) - start ))
    if (( elapsed > timeout )); then
      hb "TIMEOUT server on port=$port after ${timeout}s"
      return 1
    fi
    if (( elapsed % 30 == 0 )); then
      hb "waiting port=$port elapsed=${elapsed}s"
    fi
    sleep 2
  done
}

start_infergrid() {
  local config=$1
  local logtag=$2
  local logf="$RDIR/server_logs/${logtag}.log"
  hb "starting infergrid config=$config tag=$logtag"
  nohup python3 -m infergrid.cli serve --config "$config" --port 8000 --log-level INFO \
    > "$logf" 2>&1 &
  local pid=$!
  echo "$pid" > "$RDIR/server_logs/${logtag}.pid"
  hb "infergrid pid=$pid"
  wait_for_health 8000 420 || return 1
  # warmup call: 1 tokens, short
  curl -s -X POST http://localhost:8000/v1/completions \
    -H 'Content-Type: application/json' \
    -H 'X-Tenant-ID: warmup' \
    -d '{"model":"meta-llama/Llama-3.1-8B-Instruct","prompt":"hi","max_tokens":1}' \
    > "$RDIR/server_logs/${logtag}.warmup.json" 2>&1
  hb "warmup done tag=$logtag"
}

stop_server() {
  local logtag=$1
  local pidf="$RDIR/server_logs/${logtag}.pid"
  if [ -f "$pidf" ]; then
    local pid=$(cat "$pidf")
    hb "stopping infergrid pid=$pid tag=$logtag"
    kill "$pid" 2>/dev/null || true
    sleep 3
  fi
  pkill -9 -f "vllm.entrypoints" 2>/dev/null || true
  pkill -9 -f "EngineCore" 2>/dev/null || true
  pkill -9 -f "infergrid.cli serve" 2>/dev/null || true
  sleep 3
  # Hard-kill any lingering GPU-holding PIDs.
  local stragglers=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | awk 'NF' | head)
  for p in $stragglers; do
    kill -9 "$p" 2>/dev/null || true
  done
  sleep 4
  local gmem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
  hb "post-stop GPU used=${gmem}MiB"
}

start_direct_vllm() {
  # Launch raw vllm OpenAI server on 8100 for overhead-floor baseline.
  local logf="$RDIR/server_logs/direct_vllm.log"
  hb "starting direct vllm on 8100"
  nohup python3 -m vllm.entrypoints.openai.api_server \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --dtype bfloat16 \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.85 \
    --port 8100 --host 0.0.0.0 \
    > "$logf" 2>&1 &
  local pid=$!
  echo "$pid" > "$RDIR/server_logs/direct_vllm.pid"
  hb "direct vllm pid=$pid"
  wait_for_health 8100 420 || return 1
  curl -s -X POST http://localhost:8100/v1/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"meta-llama/Llama-3.1-8B-Instruct","prompt":"hi","max_tokens":1}' \
    > "$RDIR/server_logs/direct_vllm.warmup.json" 2>&1
  hb "direct vllm warmup done"
}

stop_direct_vllm() {
  if [ -f "$RDIR/server_logs/direct_vllm.pid" ]; then
    local pid=$(cat "$RDIR/server_logs/direct_vllm.pid")
    hb "stopping direct vllm pid=$pid"
    kill "$pid" 2>/dev/null || true
    sleep 3
  fi
  pkill -9 -f "vllm.entrypoints" 2>/dev/null || true
  pkill -9 -f "EngineCore" 2>/dev/null || true
  sleep 3
  local stragglers=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | awk 'NF' | head)
  for p in $stragglers; do
    kill -9 "$p" 2>/dev/null || true
  done
  sleep 4
  local gmem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
  hb "post-stop-direct GPU used=${gmem}MiB"
}

# Run the N-tenant bench with heartbeats.
run_bench() {
  local label=$1; local url=$2; local flooder_rps=$3; local num_quiet=$4
  local duration=$5; local outdir=$6; local extra_args=${7:-}
  mkdir -p "$outdir"
  hb "BENCH BEGIN $label rps=$flooder_rps N_quiet=$num_quiet dur=${duration}s"
  local bench_log="$outdir/bench.log"
  python3 benchmarks/scripts/benchmark_n_tenant_single_model.py \
    --url "$url" \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --flooder-rps "$flooder_rps" \
    --quiet-rps 1 \
    --num-quiet "$num_quiet" \
    --duration-s "$duration" \
    --max-tokens 64 \
    --output-dir "$outdir" \
    --seed 42 $extra_args \
    > "$bench_log" 2>&1 &
  local bpid=$!
  local start=$(date +%s)
  while kill -0 "$bpid" 2>/dev/null; do
    local el=$(( $(date +%s) - start ))
    hb "bench $label elapsed=${el}s (pid=$bpid)"
    sleep 30
  done
  wait "$bpid" || true
  local rc=$?
  hb "BENCH END $label rc=$rc"
  return $rc
}

############################################
# Phase 1 — overhead floor (solo 1T 1RPS, 120s)
############################################
PHASE=overhead
hb "================ PHASE $PHASE ================"

start_infergrid configs/gate21_fairness_n8.yaml infergrid_main
# Arm A: infergrid, solo 1 tenant at 1 RPS, flooder_rps=0 + num_quiet=1, 120s.
run_bench infergrid_solo_A http://localhost:8000 0 1 120 "$RDIR/overhead/infergrid_solo_A"
# Arm C: second infergrid pass for noise bound.
run_bench infergrid_solo_B http://localhost:8000 0 1 120 "$RDIR/overhead/infergrid_solo_B"

# Stop infergrid to free VRAM for direct vllm (single-GPU, can't coexist).
stop_server infergrid_main

start_direct_vllm
run_bench direct_vllm_solo http://localhost:8100 0 1 120 "$RDIR/overhead/direct_vllm_solo"
stop_direct_vllm

# Restart infergrid for the RPS sweep.
start_infergrid configs/gate21_fairness_n8.yaml infergrid_sweep

############################################
# Phase 2 — RPS sweep (fairness N=8, flooder_rps ∈ {16,32,64,128,256})
############################################
PHASE=rps_sweep
hb "================ PHASE $PHASE ================"

for rps in 16 32 64 128 256; do
  run_bench "rps_${rps}" http://localhost:8000 "$rps" 7 180 "$RDIR/rps_sweep/rps_${rps}"
  sleep 10
done

stop_server infergrid_sweep

############################################
# Phase 3 — FIFO anchor @ rps=128 N=8
############################################
PHASE=fifo_anchor
hb "================ PHASE $PHASE ================"

start_infergrid configs/gate22_fifo_n8.yaml fifo_n8
run_bench fifo_n8_rps128 http://localhost:8000 128 7 180 "$RDIR/fifo_anchor/rps_128"
stop_server fifo_n8

############################################
# Phase 4 — N sweep (rps=128, N ∈ {2,4,16,32})
# N=8 already run in rps_128 above.
############################################
PHASE=n_sweep
hb "================ PHASE $PHASE ================"

# Copy the N=8 result as the canonical N=8 sample
cp -r "$RDIR/rps_sweep/rps_128" "$RDIR/n_sweep/N_8"

for N in 2 4 16 32; do
  num_quiet=$((N - 1))
  max_conc=$((N * 96))  # 96 per tenant
  gmem=0.85
  adm=4096
  if (( N == 32 )); then
    max_conc=3072
    adm=8192
    gmem=0.80  # safer for N=32 under load
  fi
  cfg="$RDIR/configs/gate21_fairness_N${N}.yaml"
  python3 - <<PY
import yaml, pathlib
src = pathlib.Path('/workspace/infergrid/configs/gate21_fairness_n8.yaml').read_text()
cfg = yaml.safe_load(src)
cfg['max_concurrent'] = $max_conc
cfg['admission_queue_size'] = $adm
for m in cfg['models']:
    m['gpu_memory_utilization'] = $gmem
pathlib.Path('$cfg').write_text(yaml.safe_dump(cfg, sort_keys=False))
print("wrote $cfg max_conc=$max_conc gmem=$gmem")
PY

  start_infergrid "$cfg" "n_sweep_N${N}"
  run_bench "N_${N}_rps128" http://localhost:8000 128 "$num_quiet" 180 "$RDIR/n_sweep/N_${N}"
  stop_server "n_sweep_N${N}"
  sleep 5
done

hb "================ ALL PHASES DONE ================"
echo "ALL_PHASES_DONE" > "$RDIR/DONE"
