# 30-second terminal demo — Show HN

Two variants below. **Pick one before filming.** The GPU-live variant is more honest (real numbers in real time) but requires a running A100 or equivalent. The dry-run variant uses `kvwarden bench reproduce-hero --flavor=2tenant` against a locally running vLLM and is filmable on a laptop talking to a remote engine — or on any box where the bench harness can stream pre-recorded results.

---

## Recording guidance (both variants)

- **Resolution:** 1920x1080. Higher looks like a flex and compresses worse on Twitter.
- **Terminal:** dark theme (anything dark — iTerm2 "Solarized Dark", Alacritty default, Ghostty "Catppuccin Mocha"). Light theme washes out in social-media previews.
- **Font:** JetBrains Mono or Berkeley Mono, 18pt. Big enough that someone reading on a phone can see it without pinching.
- **Window size:** 100 columns x 30 rows. Captures the full bench output without wrapping.
- **Capture tool:** `asciinema rec` for a terminal-native recording (embeddable on Show HN / the LP), plus a screen recorder (macOS: Cmd-Shift-5, "Record Selected Portion") for the 30-second MP4 that Twitter / YouTube will accept. Record both in one take.
- **No personal info on screen:** hide the shell prompt's hostname and user. Set `PS1='$ '` before recording. Close any tabs/panes showing `patelshrey77`, email, Slack, personal dotfiles.
- **What the frame contains:** the terminal rectangle and nothing else. No desktop, no menu bar, no dock. On macOS, full-screen the terminal (Ctrl-Cmd-F) or use the record-rectangle tool to crop to the terminal bounds.
- **Voiceover:** record separately. Read at a normal pace. Re-record until the timing matches the on-screen action within ±1 second per block.

---

# Variant A — GPU-live (pick this if you have an A100 up)

Assumes kvwarden is already built and a vLLM engine is running on a reachable GPU at `$ENGINE_URL`. Preflight — not part of the recording:

```bash
# off-camera: start vLLM
vllm serve meta-llama/Llama-3.1-8B-Instruct --host 0.0.0.0 --port 8000
# off-camera: start kvwarden
kvwarden serve --config configs/gate2_fairness_token_bucket.yaml
```

## Voiceover + screen — 30s in 5-second blocks

### 0:00-0:05
**Voiceover:** "One A100. One Llama-3.1-8B engine. Two tenants sharing it."
**Terminal command:**
```bash
$ watch -n 1 'curl -s localhost:9000/metrics | grep kvwarden_p99'
```
**Viewer sees:** a live-updating metrics panel showing `kvwarden_p99_ttft_ms{tenant="quiet"}` and `...{tenant="flooder"}` both near baseline.

### 0:05-0:10
**Voiceover:** "I start the flooder at 32 requests per second. Watch the quiet tenant."
**Terminal command (new pane or tmux split):**
```bash
$ kvwarden bench run --flooder-rps=32 --quiet-rps=1 --duration=60s
```
**Viewer sees:** the bench kicks off; the metrics panel shows `kvwarden_p99_ttft_ms{tenant="quiet"}` climbing sharply past 1000ms within 10 seconds.

### 0:10-0:15
**Voiceover:** "Under vanilla FIFO, the quiet tenant's p99 TTFT hits sixteen hundred milliseconds. Starved."
**Terminal:** nothing new typed.
**Viewer sees:** the quiet-tenant p99 line stabilizes around 1500-1600ms while the flooder line sits near 200ms. The contrast is the whole point — one line is red-colored by `grep --color` or by dashboard styling.

### 0:15-0:20
**Voiceover:** "Now I flip on kvwarden's per-tenant rate limit at the admission gate. One YAML field."
**Terminal command:**
```bash
$ kvwarden config set tenants.flooder.rate_limit_rps=4 && kvwarden reload
```
**Viewer sees:** a short log line `[kvwarden] reloaded config in 12ms, admission policy: token-bucket`. The metrics panel is still running.

### 0:20-0:25
**Voiceover:** "Quiet tenant recovers in under two seconds. Sixty-one milliseconds p99. One-point-one-four times solo baseline."
**Terminal:** nothing new typed.
**Viewer sees:** the quiet-tenant p99 line drops sharply from ~1500ms to ~60ms within the visible frame. Flooder line stays flat.

### 0:25-0:30
**Voiceover:** "Ten lines of YAML. No application code. `pip install kvwarden`."
**Terminal command:**
```bash
$ pip install kvwarden
```
**Viewer sees:** pip output flashing briefly (use `--quiet` if it's too noisy), ending on `Successfully installed kvwarden-0.1.0`. Cut to black / kvwarden.org card.

---

# Variant B — dry-run (pick this if no GPU is up)

Uses `kvwarden bench reproduce-hero --flavor=2tenant` pointed at a locally running vLLM. The hero bench replays the validated Gate 2-FAIRNESS measurement and prints a side-by-side comparison against the published numbers. Ships with the CLI — no remote GPU needed if you have an engine reachable, and the harness degrades gracefully with a mock engine for demo purposes.

Preflight (off-camera):
```bash
# off-camera: any reachable vLLM at $ENGINE_URL, or the shipped mock
pip install kvwarden
```

## Voiceover + screen — 30s in 5-second blocks

### 0:00-0:05
**Voiceover:** "This is the starvation fix kvwarden ships. I'll replay it live in thirty seconds."
**Terminal command:**
```bash
$ pip install kvwarden
```
**Viewer sees:** pip install flashing past, ending on `Successfully installed kvwarden-0.1.0`. If already installed, `Requirement already satisfied` is fine.

### 0:05-0:10
**Voiceover:** "One command spins up the reproducible hero bench — 2 tenants on Llama-3.1-8B, vLLM 0.19.1."
**Terminal command:**
```bash
$ kvwarden bench reproduce-hero --flavor=2tenant
```
**Viewer sees:** startup banner — "KVWarden hero reproduction — 2tenant, 32 RPS flooder, 1 RPS quiet, 300s", plus a rich progress bar with ETA.

### 0:10-0:15
**Voiceover:** "Under vanilla FIFO, quiet-tenant p99 is sixteen hundred milliseconds. That's the baseline."
**Terminal:** no new input. The progress bar advances.
**Viewer sees:** a live-updating table with columns `tenant | condition | p99 TTFT | ref`. The `fifo / quiet` row shows `1585 ms` next to `ref: 1585 ms`.

### 0:15-0:20
**Voiceover:** "Flip on per-tenant rate limits at admission. Same workload. Same engine."
**Terminal:** still no new input — the bench runs the next phase automatically.
**Viewer sees:** the table adds a `token-bucket / quiet` row showing the p99 dropping in real time: 1200ms, 600ms, 200ms, 61ms. The delta column lights up green.

### 0:20-0:25
**Voiceover:** "Sixty-one-point-five milliseconds. Within one-point-one-four times of solo baseline. Twenty-six-x recovery."
**Terminal:** no new input.
**Viewer sees:** final comparison panel — side-by-side reference vs. observed numbers, all green checks. `OVERALL: CONFIRMS reference within tolerance.`

### 0:25-0:30
**Voiceover:** "Ten lines of YAML. No application code. kvwarden.org."
**Terminal command:**
```bash
$ open https://kvwarden.org
```
**Viewer sees:** a brief flash of the LP, then fade to black.

---

## Post-production

- **Add captions.** Social autoplay is muted by default. Burn in the voiceover as captions or use a clean sans-serif overlay (Inter, 24pt, white with 40% black drop shadow). 30 seconds of captions is about 75 words — the voiceover above is within that budget.
- **End card:** logo + kvwarden.org + "pip install kvwarden" in large text. 2 seconds. Static.
- **Output formats:** export 1:1 square (1080x1080) for Twitter/LinkedIn autoplay, and 16:9 (1920x1080) for YouTube / Hacker News embeds. Same source take, two crops.
- **File size:** keep under 10MB for the square cut so it plays natively in Twitter. H.264, 30fps, CRF 23 is the sweet spot.

## Which variant to record

- **Default: Variant A (GPU-live).** If an A100 is up and the engine is stable, this is more persuasive — the numbers move in real time and the viewer sees the fix land.
- **Fallback: Variant B (dry-run).** If the GPU is down or unreliable, the reproduce-hero flow is already validated and the numbers are truthful replays of published measurements. Acceptable for a Show HN; note "reproduced from Gate 2-FAIRNESS, reference v3" in the caption.
- **Don't mix.** Pick one. Mixing a mock and a live demo in the same take looks sloppy.
