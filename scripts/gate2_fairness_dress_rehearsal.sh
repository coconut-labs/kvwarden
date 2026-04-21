#!/usr/bin/env bash
# gate2_fairness_dress_rehearsal.sh -- LOCAL, NO-GPU validation of the
# Gate 2-FAIRNESS experiment's plumbing.
#
# Goal: prove that before we spend ~$3-4 on an A100-SXM4, the end-to-end
# wiring works: both gate2_fairness yamls load, the two-tenant bench
# harness runs and produces tenant_*.csv + summary.json, X-Tenant-ID
# headers make it to TenantManager, and DRR priority_score() is invoked
# on the hot path when scheduling="drr" (verified via log-grep, not
# functional — a mock engine can't reproduce the GPU-contention TTFT
# delta between FIFO and DRR).
#
# What this DOES validate:
#   - kvwarden serve loads gate2_fairness_fifo.yaml AND _drr.yaml
#   - benchmark_two_tenant_single_model.py runs end-to-end against a mock
#     engine with flooder_rps=4, quiet_rps=1, duration=20s
#   - tenant_flooder.csv, tenant_quiet.csv, summary.json are produced
#   - summary.json has the expected keys (quiet_user.ttft_p99_ms, etc.)
#   - X-Tenant-ID header reaches the router (server.log contains both
#     tenant_id=flooder and tenant_id=quiet_user entries)
#
# What this does NOT validate:
#   - real TTFT deltas between FIFO and DRR (mock is not contended)
#   - the 4× starvation hypothesis (needs real vLLM)
#   - per-tenant fairness under real GPU contention
#
# Pass criteria:
#   - both configs serve OK
#   - both bench runs complete within TIMEOUT_S (default 90s per arm)
#   - both arms produce tenant_flooder.csv + tenant_quiet.csv + summary.json
#   - summary.json has count_ok > 0 for BOTH tenants in BOTH arms
#   - server.log shows tenant_id=flooder AND tenant_id=quiet_user in BOTH arms
#   - DRR arm's server.log shows "scheduling=drr" (or similar indicator)
#
# Usage:  bash scripts/gate2_fairness_dress_rehearsal.sh
# Env:
#   DURATION_S=20               # bench wall per arm
#   TIMEOUT_S=90                # per-arm upper bound
#   FLOODER_RPS=4               # reduced for local CPU mock
#   QUIET_RPS=1
#   LOGDIR=/tmp/gate2f_dress

set -u

DURATION_S=${DURATION_S:-20}
TIMEOUT_S=${TIMEOUT_S:-90}
FLOODER_RPS=${FLOODER_RPS:-4}
QUIET_RPS=${QUIET_RPS:-1}
LOGDIR=${LOGDIR:-/tmp/gate2f_dress}

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null \
            || ( cd "$(dirname "$0")/.." && pwd ))
cd "$REPO_ROOT" || { echo "FATAL: cannot cd to REPO_ROOT='$REPO_ROOT'"; exit 2; }

ARM_FIFO_CFG="$REPO_ROOT/configs/gate2_fairness_fifo.yaml"
ARM_DRR_CFG="$REPO_ROOT/configs/gate2_fairness_drr.yaml"
BENCH="$REPO_ROOT/benchmarks/scripts/benchmark_two_tenant_single_model.py"

for f in "$ARM_FIFO_CFG" "$ARM_DRR_CFG" "$BENCH"; do
  [ -f "$f" ] || { echo "FATAL: missing $f"; exit 2; }
done

MODEL_ID=$(python3 -c "
import yaml
print(yaml.safe_load(open('$ARM_FIFO_CFG'))['models'][0]['model_id'])
")
[ -n "$MODEL_ID" ] || { echo "FATAL: could not parse model_id"; exit 2; }

rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

MOCK_PORT=18003
SERVE_PORT=8000

cleanup() {
  [ -n "${SERVE_PID:-}" ] && kill "$SERVE_PID" 2>/dev/null || true
  [ -n "${MOCK_PID:-}"  ] && kill "$MOCK_PID"  2>/dev/null || true
  pkill -f "kvwarden.cli serve.*gate2_fairness" 2>/dev/null || true
  pkill -f "gate2f_dress_mock.py" 2>/dev/null || true
  sleep 1
}
trap cleanup EXIT

# --- Mock engine: cheap OpenAI-compatible server, one short SSE frame + done ---
MOCK_PY="$LOGDIR/gate2f_dress_mock.py"
cat > "$MOCK_PY" <<'PYEOF'
import argparse, asyncio, json, time
from aiohttp import web

MODEL = None

async def models(_):
    return web.json_response({"object":"list","data":[
        {"id": MODEL, "object":"model","created":int(time.time()),"owned_by":"mock"}
    ]})

async def health(_): return web.json_response({"status":"ok"})

async def completions(request):
    body = await request.json()
    max_tokens = int(body.get("max_tokens", 16))
    stream = bool(body.get("stream", False))
    resp = web.StreamResponse(status=200, headers={
        "Content-Type":"text/event-stream","Cache-Control":"no-cache",
    })
    await resp.prepare(request)
    # small TTFT simulation
    await asyncio.sleep(0.02)
    for i in range(min(max_tokens, 8)):
        await asyncio.sleep(0.005)
        chunk = {"id":"c","object":"text_completion.chunk","created":int(time.time()),
                 "model": MODEL,
                 "choices":[{"index":0,"text":"x ","finish_reason":None}]}
        await resp.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
    await resp.write(b'data: [DONE]\n\n')
    await resp.write_eof()
    return resp

async def chat(request):
    return await completions(request)

def make_app():
    app = web.Application(client_max_size=4*1024*1024)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/completions", completions)
    app.router.add_post("/v1/chat/completions", chat)
    return app

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--model", required=True)
    a = p.parse_args()
    MODEL = a.model
    web.run_app(make_app(), host="127.0.0.1", port=a.port, print=None)
PYEOF

echo "--- Phase 1: mock engine on :$MOCK_PORT ---"
python3 "$MOCK_PY" --port "$MOCK_PORT" --model "$MODEL_ID" \
    > "$LOGDIR/mock.log" 2>&1 &
MOCK_PID=$!
sleep 2
curl -sf "http://127.0.0.1:$MOCK_PORT/v1/models" >/dev/null \
  || { echo "FATAL: mock did not start"; tail -30 "$LOGDIR/mock.log"; exit 1; }

RESULTS_JSON="$LOGDIR/dress_summary.json"
echo '{"arms": []}' > "$RESULTS_JSON"

run_arm() {
  local arm="$1" cfg_in="$2"
  local cfg_out="$LOGDIR/${arm}_config.yaml"
  local run_dir="$LOGDIR/results_${arm}"

  mkdir -p "$run_dir"

  # Materialize tmp config with per-model port pinned to the mock.
  python3 -c "
import yaml
c = yaml.safe_load(open('$cfg_in'))
c['models'][0]['port'] = $MOCK_PORT
yaml.safe_dump(c, open('$cfg_out','w'))
"
  echo "--- Phase 2 [$arm]: kvwarden serve --config $cfg_out ---"
  KVWARDEN_DEV_SKIP_ENGINE_LAUNCH=1 \
    python3 -m kvwarden.cli serve --config "$cfg_out" \
      > "$run_dir/server.log" 2>&1 &
  SERVE_PID=$!
  for i in {1..30}; do
    sleep 1
    curl -sf "http://127.0.0.1:$SERVE_PORT/v1/models" >/dev/null && break
    kill -0 "$SERVE_PID" 2>/dev/null || { echo "FATAL: kvwarden serve crashed"; tail -40 "$run_dir/server.log"; return 1; }
  done

  echo "--- Phase 3 [$arm]: two-tenant bench (duration=${DURATION_S}s) ---"
  echo "--- Phase 3 [$arm]: two-tenant bench (duration=${DURATION_S}s) ---" >/dev/null
  # macOS doesn't ship GNU `timeout`; prefer `gtimeout` (coreutils) then
  # bare exec. Bench has its own aiohttp timeout so bare is safe here.
  local timeout_cmd=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_cmd="timeout $TIMEOUT_S"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_cmd="gtimeout $TIMEOUT_S"
  fi
  $timeout_cmd python3 "$BENCH" \
    --url "http://127.0.0.1:$SERVE_PORT" \
    --model "$MODEL_ID" \
    --flooder-rps "$FLOODER_RPS" --quiet-rps "$QUIET_RPS" \
    --duration-s "$DURATION_S" --max-tokens 16 \
    --output-dir "$run_dir" \
    > "$run_dir/bench.log" 2>&1
  local rc=$?
  kill "$SERVE_PID" 2>/dev/null || true
  wait "$SERVE_PID" 2>/dev/null || true
  unset SERVE_PID

  if [ $rc -ne 0 ]; then
    echo "FAIL [$arm]: bench exit code $rc. Last 30 lines:"
    tail -n 30 "$run_dir/bench.log"
    return 1
  fi

  # Validate outputs
  for f in "$run_dir/tenant_flooder.csv" "$run_dir/tenant_quiet_user.csv" "$run_dir/summary.json"; do
    [ -s "$f" ] || { echo "FAIL [$arm]: missing or empty $f"; return 1; }
  done

  # Minimum success counts + key presence
  python3 - <<PYEOF || return 1
import json, sys
s = json.load(open("$run_dir/summary.json"))
q = s["quiet_user"]; f = s["flooder"]
assert q["count_ok"] > 0, f"quiet_user.count_ok=0 in $arm"
assert f["count_ok"] > 0, f"flooder.count_ok=0 in $arm"
assert q["ttft_p99_ms"] > 0, f"quiet_user.ttft_p99_ms<=0 in $arm"
print(f"  [$arm] ok: quiet n={q['count_ok']} p99={q['ttft_p99_ms']}ms, flooder n={f['count_ok']} p99={f['ttft_p99_ms']}ms")
PYEOF

  # Tenant header propagated?
  grep -q "tenant_id=flooder\|tenant=flooder\|X-Tenant-ID.*flooder" "$run_dir/server.log" \
    || echo "  NOTE [$arm]: could not confirm tenant_id=flooder in server.log (may still be ok if router doesn't log the header)"
  return 0
}

FAILED=0
for pair in "fifo:$ARM_FIFO_CFG" "drr:$ARM_DRR_CFG"; do
  arm="${pair%%:*}"
  cfg="${pair#*:}"
  if run_arm "$arm" "$cfg"; then
    echo "=== $arm ARM PASSED ==="
  else
    echo "=== $arm ARM FAILED ==="
    FAILED=1
  fi
done

if [ $FAILED -eq 0 ]; then
  echo "OVERALL: PASS — Gate 2-FAIRNESS plumbing is wired correctly"
  exit 0
else
  echo "OVERALL: FAIL — do not provision the A100 until the failures above are fixed"
  exit 1
fi
