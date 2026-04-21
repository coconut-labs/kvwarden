# Reproduce the hero number

`kvwarden bench reproduce-hero` runs the same 300-second noisy-neighbor
bench that produced the numbers in the [launch post](launch/gate0_launch_post.md),
then prints your box's result side-by-side with the published reference.

## Quick start (local, server already up)

```bash
# 1. Start a server in another terminal against the hero config.
kvwarden serve --config configs/gate2_fairness_token_bucket.yaml --port 8000

# 2. Wait for /health to return 200 (first call is 503 while vLLM JIT-compiles).
until curl -fs http://localhost:8000/health > /dev/null; do sleep 5; done

# 3. Run the hero bench.
kvwarden bench reproduce-hero
```

Finishes in ~5 min. Writes results to `./kvwarden-reproduce-<timestamp>/`.

## Flavors

| Flag | Tenants | Published quiet p99 | Ratio vs solo |
|---|---|---:|---:|
| `--flavor 2tenant` (default) | 1 flooder + 1 quiet | 61.5 ms | 1.14x |
| `--flavor n6`                | 1 flooder + 5 quiet | 61.0 ms | 1.13x |
| `--flavor n8`                | 1 flooder + 7 quiet | 50.4 ms | 1.05x |

Each flavor expects its matching YAML on the server side:

- 2tenant: `configs/gate2_fairness_token_bucket.yaml`
- n6: `configs/gate2_fairness_token_bucket_n6.yaml`
- n8: `configs/gate21_fairness_n8.yaml`

## Provision a pod for you (`--pod`)

If `RUNPOD_API_KEY` is set, `--pod` spins up a 1x A100 SXM pod, rsyncs
the results back, and deletes the pod on exit (including on Ctrl-C):

```bash
export RUNPOD_API_KEY=<your key>
kvwarden bench reproduce-hero --pod
```

Pass `--no-delete` to keep the pod for post-run inspection.

Total wall time (pod path): ~25 min including pull + warmup + bench + teardown.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--flavor` | `2tenant` | `2tenant`, `n6`, or `n8` |
| `--duration-s` | `300` | Bench wall time per flavor |
| `--base-url` | `http://localhost:8000` | KVWarden server URL |
| `--pod` | off | Provision a RunPod A100 (requires `RUNPOD_API_KEY`) |
| `--no-delete` | off | Don't delete the pod after a `--pod` run |

## Report format

`./kvwarden-reproduce-<timestamp>/report.json` contains your
measurement, the reference, and the delta ratio.

```json
{
  "schema_version": 1,
  "flavor": "2tenant",
  "user_result": {
    "quiet_aggregate_p99_ms": 61.8,
    "flooder_p99_ms": 1720.0,
    "flooder_429_rate": 0.94
  },
  "reference": {
    "tokenbucket_p99_ms": 61.5,
    "ratio_of_solo": 1.14,
    "source": "results/gate2_preprint_v3/"
  }
}
```

## Common errors

- **`nothing listening on localhost:8000`**: server not up. Run
  `kvwarden serve --config <flavor config>` in a second terminal.
- **`Model ... not found in /v1/models`**: your config doesn't include
  `meta-llama/Llama-3.1-8B-Instruct`. Use one of the flavor configs
  above.
- **`HTTP 503 on /health`**: vLLM is still JIT-compiling (typical on
  first request; 30-90 s on A100 for Llama-3.1-8B). Wait and re-run.

## When your numbers diverge from published

Expected if:

- GPU differs from A100-SXM4 (the table warns on mismatch).
- Your engine build is not `vllm==0.19.1` (the hero-validated pin).
- You ran fewer than 300 s (insufficient samples for stable p99).

File an issue with `tenant_*.csv` attached if the published arm is
2x+ from reference on an A100 / `vllm==0.19.1`.
