#!/usr/bin/env bash
# gate1_dress_rehearsal.sh -- LOCAL, NO-GPU dry-run of the Gate 1 experiment.
#
# Goal: prove the Gate 1 plumbing (configs, bench harness, /metrics scrape,
# admission counters, JSON output, discriminator math) works end-to-end BEFORE
# spending $7-10 on an H100 SXM5 spot pod.
#
# What this DOES validate:
#   - infergrid serve loads BOTH gate1 configs (admission ON / admission OFF)
#   - benchmark_multi_model.py runs at c=128 and c=256 against a mock backend
#   - /metrics admission_in_flight gauge engages on Arm A, stays open on Arm B
#   - per-(arm, concurrency) ttft_p99_ms is parsed cleanly from summary JSON
#   - the discriminator math runs and prints PASS/FAIL the way Gate 1 will
#
# What this does NOT validate:
#   - real H100 scheduler/KV behavior. The mock backend below is TUNED to
#     produce a knee at in_flight>128 so the discriminator passes. That makes
#     this a SCAFFOLDING self-test of the experiment, not a hypothesis test.
#   - real vLLM warmup, real GPU memory budget, real engine bring-up.
#
# Pass criteria (all must hold):
#   - both configs serve OK in dev-skip mode
#   - 4 bench runs (2 arms x c=128,256) complete with non-zero throughput
#   - Arm A peak in_flight gauge clamps near max_concurrent=128
#   - Arm B peak in_flight gauge exceeds 128 (admission OPEN)
#   - discriminator: A_p99(c=256) <= 2*A_p99(c=128) AND B_p99(c=256) >= 4*A_p99(c=256)
#
# Usage:
#   bash scripts/gate1_dress_rehearsal.sh
# Env:
#   NUM_REQUESTS=400          # per (arm, concurrency) combo
#   LOGDIR=/tmp/gate1_dress
#   MOCK_BASE_TTFT_S=0.05     # delay-first-content for in_flight <= knee
#   MOCK_KNEE=128             # in_flight at which TTFT starts climbing
#   MOCK_PER_EXCESS_S=0.012   # added per request above the knee
#   SKIP_DISCRIMINATOR=0      # set 1 to plumbing-only run (no PASS/FAIL on math)

set -u

NUM_REQUESTS=${NUM_REQUESTS:-400}
LOGDIR=${LOGDIR:-/tmp/gate1_dress}
MOCK_BASE_TTFT_S=${MOCK_BASE_TTFT_S:-0.05}
MOCK_KNEE=${MOCK_KNEE:-128}
MOCK_PER_EXCESS_S=${MOCK_PER_EXCESS_S:-0.012}
SKIP_DISCRIMINATOR=${SKIP_DISCRIMINATOR:-0}

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null \
            || ( cd "$(dirname "$0")/.." && pwd ))
cd "$REPO_ROOT" || { echo "FATAL: cannot cd to REPO_ROOT='$REPO_ROOT'"; exit 2; }

ARM_A_CFG="$REPO_ROOT/configs/gate1_admission.yaml"
ARM_B_CFG="$REPO_ROOT/configs/gate1_admission_off.yaml"
[ -f "$ARM_A_CFG" ] || { echo "FATAL: missing $ARM_A_CFG"; exit 2; }
[ -f "$ARM_B_CFG" ] || { echo "FATAL: missing $ARM_B_CFG"; exit 2; }

# Read model_id from gate1_admission.yaml so the mock advertises the same id.
MODEL_ID=$(python3 -c "
import yaml,sys
with open('$ARM_A_CFG') as f: c = yaml.safe_load(f)
print(c['models'][0]['model_id'])
")
[ -n "$MODEL_ID" ] || { echo "FATAL: could not parse model_id"; exit 2; }

mkdir -p "$LOGDIR"
rm -rf "$LOGDIR"/results "$LOGDIR"/*.log "$LOGDIR"/*.csv "$LOGDIR"/*.json 2>/dev/null
mkdir -p "$LOGDIR/results"

MOCK_PORT=18002   # arbitrary local port for the load-aware mock
SERVE_PORT=8000

cleanup() {
  echo "--- cleanup ---"
  [ -n "${POLLER_PID:-}" ] && kill "$POLLER_PID" 2>/dev/null || true
  [ -n "${SERVE_PID:-}"  ] && kill "$SERVE_PID"  2>/dev/null || true
  [ -n "${MOCK_PID:-}"   ] && kill "$MOCK_PID"   2>/dev/null || true
  pkill -f "infergrid.cli serve.*gate1_" 2>/dev/null || true
  pkill -f "gate1_dress_mock.py"          2>/dev/null || true
  sleep 1
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Emit a load-aware mock engine. Tracks in_flight; TTFT = base + max(0,
#    in_flight - knee) * per_excess_s. This is the ONLY way the assertion
#    "B_p99(c=256) >= 4*A_p99(c=256)" can be true with a mock backend, since
#    on a real H100 the knee comes from KV pressure (not modelable in CPU).
# ---------------------------------------------------------------------------
MOCK_PY="$LOGDIR/gate1_dress_mock.py"
cat > "$MOCK_PY" <<'PYEOF'
"""Load-aware mock vLLM engine for Gate 1 dress rehearsal.

OpenAI-compatible enough for infergrid's vLLMEngine adapter + the multi-model
bench harness. Tracks concurrent in-flight requests and adds knee-shaped
TTFT pressure so admission ON vs OFF produces a measurable difference.
"""
from __future__ import annotations
import argparse, asyncio, json, os, time
from aiohttp import web

class State:
    def __init__(self, model, base_s, knee, per_excess_s, tok_latency_s):
        self.model = model
        self.base_s = base_s
        self.knee = knee
        self.per_excess_s = per_excess_s
        self.tok_latency_s = tok_latency_s
        self.in_flight = 0
        self.peak_in_flight = 0

async def models(request):
    s = request.app["s"]
    return web.json_response({"object": "list", "data": [
        {"id": s.model, "object": "model", "created": int(time.time()), "owned_by": "mock"}
    ]})

async def health(_):
    return web.json_response({"status": "ok"})

async def completions(request):
    s: State = request.app["s"]
    s.in_flight += 1
    if s.in_flight > s.peak_in_flight: s.peak_in_flight = s.in_flight
    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        max_tokens = int(body.get("max_tokens", 32))
        stream = bool(body.get("stream", False))
        ttft = s.base_s + max(0, s.in_flight - s.knee) * s.per_excess_s

        if not stream:
            await asyncio.sleep(ttft + max_tokens * s.tok_latency_s)
            return web.json_response({
                "id": "cmpl-mock", "object": "text_completion",
                "created": int(time.time()), "model": s.model,
                "choices": [{"index": 0, "text": "x " * max_tokens, "finish_reason": "length"}],
                "usage": {"prompt_tokens": len(prompt.split()),
                          "completion_tokens": max_tokens,
                          "total_tokens": len(prompt.split()) + max_tokens},
            })

        # Streaming SSE: empty frame, then ttft sleep, then per-token frames.
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await resp.prepare(request)
        # Empty-text frame (lets clients start their TTFT clock).
        await resp.write(b'data: ' + json.dumps({
            "id": "cmpl-mock", "object": "text_completion.chunk",
            "created": int(time.time()), "model": s.model,
            "choices": [{"index": 0, "text": "", "finish_reason": None}],
        }).encode() + b'\n\n')
        await asyncio.sleep(ttft)
        for i in range(max_tokens):
            await asyncio.sleep(s.tok_latency_s)
            await resp.write(b'data: ' + json.dumps({
                "id": "cmpl-mock", "object": "text_completion.chunk",
                "created": int(time.time()), "model": s.model,
                "choices": [{"index": 0, "text": "x ", "finish_reason": None}],
            }).encode() + b'\n\n')
        await resp.write(b'data: ' + json.dumps({
            "id": "cmpl-mock", "object": "text_completion.chunk",
            "created": int(time.time()), "model": s.model,
            "choices": [{"index": 0, "text": "", "finish_reason": "length"}],
        }).encode() + b'\n\n')
        await resp.write(b'data: [DONE]\n\n')
        await resp.write_eof()
        return resp
    finally:
        s.in_flight -= 1

async def chat(request):
    body = await request.json()
    body["prompt"] = " ".join(m.get("content", "") for m in body.get("messages", []))
    request._read_bytes = json.dumps(body).encode()
    return await completions(request)

async def stats(request):
    s = request.app["s"]
    return web.json_response({"in_flight": s.in_flight, "peak_in_flight": s.peak_in_flight})

def make_app(s):
    app = web.Application(client_max_size=1024 * 1024 * 16)
    app["s"] = s
    app.router.add_get("/v1/models", models)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/completions", completions)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/_mock/stats", stats)
    return app

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--base-ttft-s", type=float, default=0.05)
    p.add_argument("--knee", type=int, default=128)
    p.add_argument("--per-excess-s", type=float, default=0.012)
    p.add_argument("--tok-latency-s", type=float, default=0.010)
    a = p.parse_args()
    web.run_app(
        make_app(State(a.model, a.base_ttft_s, a.knee, a.per_excess_s, a.tok_latency_s)),
        host="127.0.0.1", port=a.port, print=None,
    )
PYEOF

# ---------------------------------------------------------------------------
# 2. Start the load-aware mock engine ONCE; it serves all 4 bench runs.
#    infergrid runs in DEV_SKIP_ENGINE_LAUNCH mode; the model client adapter
#    talks to whatever is on the per-model port. We override the per-model
#    port via a tmp config (gate1 configs leave port=auto).
# ---------------------------------------------------------------------------
echo "--- Phase 1: load-aware mock engine on :$MOCK_PORT for $MODEL_ID ---"
echo "    knee=$MOCK_KNEE base=${MOCK_BASE_TTFT_S}s per_excess=${MOCK_PER_EXCESS_S}s"
python3 "$MOCK_PY" --port "$MOCK_PORT" --model "$MODEL_ID" \
    --base-ttft-s "$MOCK_BASE_TTFT_S" --knee "$MOCK_KNEE" \
    --per-excess-s "$MOCK_PER_EXCESS_S" --tok-latency-s 0.010 \
    > "$LOGDIR/mock.log" 2>&1 &
MOCK_PID=$!
sleep 2
curl -sf "http://127.0.0.1:$MOCK_PORT/v1/models" >/dev/null \
  || { echo "FATAL: mock engine did not start; tail:"; tail -30 "$LOGDIR/mock.log"; exit 1; }

# ---------------------------------------------------------------------------
# 3. Per-arm rehearsal driver. Re-uses the gate1 configs verbatim, but
#    materializes a tmp variant with the per-model port pinned to MOCK_PORT
#    so the dev-skip engine adapter connects to the load-aware mock.
# ---------------------------------------------------------------------------
RESULTS_JSON="$LOGDIR/results/discriminator.json"
echo '{"runs": []}' > "$RESULTS_JSON"

run_arm() {
  local arm="$1" cfg_in="$2" max_conc="$3"
  local cfg_out="$LOGDIR/${arm}_config.yaml"
  python3 -c "
import yaml,sys
with open('$cfg_in') as f: c = yaml.safe_load(f)
c['models'][0]['port'] = $MOCK_PORT
with open('$cfg_out','w') as f: yaml.safe_dump(c, f)
"
  echo "--- Phase 2.$arm: serve $cfg_in (max_concurrent=$max_conc) ---"
  INFERGRID_DEV_SKIP_ENGINE_LAUNCH=1 \
  PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}" \
  python3 -m infergrid.cli serve --config "$cfg_out" --log-level WARNING \
      > "$LOGDIR/serve_${arm}.log" 2>&1 &
  SERVE_PID=$!

  # Wait for /v1/models to be live.
  local ready=0
  for _ in $(seq 1 30); do
    if curl -sf "http://localhost:$SERVE_PORT/v1/models" 2>/dev/null \
        | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('data') else 1)" 2>/dev/null; then
      ready=1; break
    fi
    sleep 1
  done
  [ $ready = 1 ] || { echo "FAIL: $arm /v1/models not ready"; tail -40 "$LOGDIR/serve_${arm}.log"; return 1; }

  for conc in 128 256; do
    local tag="${arm}_c${conc}"
    local outdir="$LOGDIR/results/$tag"
    mkdir -p "$outdir"

    # Background poller: peak in_flight + queue_depth from /metrics.
    local trace="$LOGDIR/results/${tag}_admission.csv"
    echo "ts,in_flight,queue_depth,admitted,rejected" > "$trace"
    (
      while true; do
        M=$(curl -sf "http://localhost:$SERVE_PORT/metrics" 2>/dev/null) || M=""
        IF=$(printf '%s\n' "$M" | awk '/^infergrid_admission_in_flight /{print $2; exit}')
        QD=$(printf '%s\n' "$M" | awk '/^infergrid_admission_queue_depth /{print $2; exit}')
        AT=$(printf '%s\n' "$M" | awk '/^infergrid_admission_admitted_total /{print $2; exit}')
        RT=$(printf '%s\n' "$M" | awk '/^infergrid_admission_rejected_total\{/{sum+=$2} END{print sum+0}')
        printf "%s,%s,%s,%s,%s\n" "$(date +%s.%N)" "${IF:-0}" "${QD:-0}" "${AT:-0}" "${RT:-0}" >> "$trace"
        sleep 0.05
      done
    ) > /dev/null 2>&1 &
    POLLER_PID=$!

    echo "    bench $tag: c=$conc num=$NUM_REQUESTS"
    python3 benchmarks/scripts/benchmark_multi_model.py \
        --url "http://localhost:$SERVE_PORT" \
        --models "$MODEL_ID" \
        --workload concurrent --concurrency "$conc" \
        --num-requests "$NUM_REQUESTS" \
        --output-dir "$outdir" --seed 42 \
        > "$LOGDIR/results/${tag}_bench.log" 2>&1
    local rc=$?

    kill "$POLLER_PID" 2>/dev/null || true; wait "$POLLER_PID" 2>/dev/null || true

    # Extract ttft_p99_ms from summary; record peak in_flight.
    local sumfile="$outdir/concurrent_c${conc}_summary.json"
    local p99=$(python3 -c "
import json,sys
try:
    d = json.load(open('$sumfile'))
    print(d.get('ttft_p99_ms', -1))
except Exception:
    print(-1)
")
    local peak_if=$(awk -F, 'NR>1 && $2+0>p{p=$2+0} END{printf "%.0f", p+0}' "$trace")
    local peak_qd=$(awk -F, 'NR>1 && $3+0>p{p=$3+0} END{printf "%.0f", p+0}' "$trace")
    echo "    => $tag rc=$rc ttft_p99=${p99}ms peak_in_flight=$peak_if peak_qd=$peak_qd"

    python3 -c "
import json
d = json.load(open('$RESULTS_JSON'))
d['runs'].append({'arm':'$arm','concurrency':$conc,'rc':$rc,
                  'ttft_p99_ms':float('$p99'),'peak_in_flight':int('$peak_if'),
                  'peak_queue_depth':int('$peak_qd'),'max_concurrent':$max_conc})
json.dump(d, open('$RESULTS_JSON','w'), indent=2)
"
  done

  kill "$SERVE_PID" 2>/dev/null || true; wait "$SERVE_PID" 2>/dev/null || true
  pkill -f "infergrid.cli serve.*${arm}_config" 2>/dev/null || true
  sleep 2   # let port :8000 free up before the next arm
}

run_arm A "$ARM_A_CFG" 128 || exit 1
run_arm B "$ARM_B_CFG" 1024 || exit 1

# ---------------------------------------------------------------------------
# 4. Discriminator + plumbing assertions.
# ---------------------------------------------------------------------------
echo "═══════════════════════════════════════════════════════"
echo " GATE 1 DRESS REHEARSAL RESULTS"
echo "═══════════════════════════════════════════════════════"
python3 - "$RESULTS_JSON" "$SKIP_DISCRIMINATOR" <<'PYEOF'
import json, sys
runs = json.load(open(sys.argv[1]))['runs']
skip_disc = sys.argv[2] == "1"
def get(arm, c):
    for r in runs:
        if r['arm'] == arm and r['concurrency'] == c: return r
    return None
A128, A256 = get('A',128), get('A',256)
B128, B256 = get('B',128), get('B',256)
fails = []
for r in (A128, A256, B128, B256):
    print(f"  {r['arm']} c={r['concurrency']}: ttft_p99={r['ttft_p99_ms']:.1f}ms "
          f"peak_in_flight={r['peak_in_flight']} peak_qd={r['peak_queue_depth']} rc={r['rc']}")
    if r['rc'] != 0 or r['ttft_p99_ms'] < 0:
        fails.append(f"{r['arm']} c={r['concurrency']} bench failed")
# Plumbing: A clamps near 128, B exceeds 128.
if A256['peak_in_flight'] > 160:
    fails.append(f"Arm A admission did NOT clamp at c=256 (peak_in_flight={A256['peak_in_flight']}, expected ~128)")
if B256['peak_in_flight'] < 160:
    fails.append(f"Arm B admission did NOT stay open at c=256 (peak_in_flight={B256['peak_in_flight']}, expected >160)")
# Discriminator (matches Gate 1 hypothesis exactly).
if not skip_disc:
    a_ratio = A256['ttft_p99_ms'] / max(A128['ttft_p99_ms'], 0.001)
    b_vs_a  = B256['ttft_p99_ms'] / max(A256['ttft_p99_ms'], 0.001)
    print(f"\n  Discriminator: A_p99(256)/A_p99(128) = {a_ratio:.2f}x  (need <= 2.00x)")
    print(f"  Discriminator: B_p99(256)/A_p99(256) = {b_vs_a:.2f}x  (need >= 4.00x)")
    if a_ratio > 2.0: fails.append(f"A_p99 ratio {a_ratio:.2f}x > 2.0")
    if b_vs_a  < 4.0: fails.append(f"B/A_p99 ratio {b_vs_a:.2f}x < 4.0")
if fails:
    print("\nOVERALL: FAIL"); [print("  -", f) for f in fails]; sys.exit(2)
print("\nOVERALL: PASS — Gate 1 plumbing is wired correctly. Greenlight pod spin.")
PYEOF
RC=$?
echo ""
echo "  Logs: $LOGDIR/"
echo "  Results JSON: $RESULTS_JSON"
exit $RC
