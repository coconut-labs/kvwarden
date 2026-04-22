# 30-second terminal demo — Show HN

Two variants below. **Pick one before filming.** Variant A needs an A100 (or equivalent) up with a vLLM engine reachable, because it shows a real `reproduce-hero` run edited down to 30s. Variant B is a CLI walkthrough that needs no GPU — it's honest about being documentation-level rather than live-bench-level.

Both variants use only commands that exist today in `src/kvwarden/cli.py` and output formats that `kvwarden bench reproduce-hero` actually produces. No aspirational commands, no invented metrics.

---

## Recording guidance (both variants)

- **Resolution:** 1920x1080. Bigger looks like a flex and compresses worse on Twitter.
- **Terminal:** dark theme (iTerm2 "Solarized Dark", Alacritty default, Ghostty "Catppuccin Mocha"). Light themes wash out in social-media previews.
- **Font:** JetBrains Mono or Berkeley Mono, 18pt. Big enough that someone reading on a phone can see without pinching.
- **Window size:** 100 columns x 30 rows. Fits the reproduce-hero output without wrapping.
- **Capture:** `asciinema rec` for the terminal-native recording (embeddable on Show HN / the LP), plus a screen recorder (macOS: Cmd-Shift-5, "Record Selected Portion") for the 30s MP4 that Twitter / YouTube will accept. Record both in the same take so they stay in sync.
- **No personal info on screen:** hide hostname and user. `PS1='$ '` before recording. Close tabs/panes showing `patelshrey77`, email, Slack, dotfiles.
- **Frame contains terminal only:** no desktop, no menu bar, no dock. Full-screen the terminal (macOS: Ctrl-Cmd-F) or crop to the terminal rectangle with Cmd-Shift-5.
- **Voiceover:** record separately. Normal pace. Re-record until on-screen action and audio align within ±1s per block.

---

# Variant A — GPU-live (time-lapse, pick if an A100 is up)

`kvwarden bench reproduce-hero --flavor=2tenant` takes ~5 minutes end-to-end (pre-flight + 300s bench + post-processing). The 30s demo is an **edited highlight reel** — time-lapse or speed-ramp the progress-bar phase, hold on the final comparison table. This matches the published Gate 2-FAIRNESS replication; no invented commands.

Preflight (off-camera):
```bash
# terminal A: serve a token-bucket config
kvwarden serve --config configs/gate2_fairness_token_bucket.yaml
# wait for `curl -fs http://localhost:9000/health` to return 200
```

Filming (terminal B, recorded):
```bash
$ kvwarden bench reproduce-hero --flavor=2tenant
```

## Voiceover + screen — 30s across 4 blocks (edit to fit)

### 0:00-0:05 — open
**Voiceover:** "One A100. One Llama-3.1-8B engine on vLLM 0.19.1. Two tenants sharing it — one flooder at 32 RPS, one quiet user at 1 RPS."
**Viewer sees:** the start of the reproduce-hero run — banner line showing flavor, RPS, duration, plus the Rich progress bar for "bench flavor=2-tenant" at `0/300s`.

### 0:05-0:15 — the bench (time-lapsed)
**Voiceover:** "Under vanilla FIFO the quiet tenant gets starved. With kvwarden's admission gate — one YAML knob — the same workload recovers."
**Editing:** speed-ramp the 300s of progress bar advancing into about 8-10s of footage. Keep the ETA field visible so viewers see the bar is real, just compressed.
**Viewer sees:** the progress bar sweeping `0/300s` to `300/300s` with the ETA counting down.

### 0:15-0:25 — the result
**Voiceover:** "Quiet-tenant p99 TTFT lands at sixty-one-point-five milliseconds. One-point-one-four times solo baseline. The FIFO reference from the same config was sixteen-hundred milliseconds — twenty-six-times worse."
**Viewer sees:** the final Rich comparison table rendered by `reproduce-hero` — observed column vs `REFERENCES["2tenant"]` column, the quiet-tenant row highlighted. The closing line `OVERALL: CONFIRMS reference within tolerance` is the hero frame — hold on it for 3 full seconds.

### 0:25-0:30 — CTA
**Voiceover:** "Ten lines of YAML. No application code. `pip install kvwarden`."
**Terminal command:**
```bash
$ pip install kvwarden
```
**Viewer sees:** pip flashes `Successfully installed kvwarden-0.1.0`, then cut to a static kvwarden.org end-card.

> **Note:** the last line shows `kvwarden-0.1.0`, which only lands on PyPI when the real 0.1.0 release ships. If you're filming before the PyPI upload, substitute `Successfully installed kvwarden-0.0.1` (the stub), or re-record the last second after the real upload.

---

# Variant B — CLI walkthrough (pick if no GPU is up)

No bench run, no engine. This is a 30-second CLI tour that validates the install and previews the bench command surface without actually hitting a GPU. Less persuasive than Variant A, but honest and filmable on a laptop in 10 minutes. Acceptable for a Show HN when time/GPU-availability blocks Variant A.

Preflight:
```bash
pip install kvwarden
```

## Voiceover + screen — 30s across 4 blocks

### 0:00-0:07 — install
**Voiceover:** "kvwarden ships as a pip install. Eight-second setup on any Python 3.11+ machine."
**Terminal commands:**
```bash
$ pip install kvwarden
$ kvwarden --help
```
**Viewer sees:** `Successfully installed kvwarden-0.1.0`, then the top-level `kvwarden --help` output showing the subcommand list (`serve`, `bench`, `status`, `models`, `man`, `telemetry`).

### 0:07-0:15 — bench command
**Voiceover:** "The headline bench replays the published measurement — two tenants, Llama-3.1-8B, vLLM 0.19.1 — against any vLLM-compatible engine you point it at."
**Terminal command:**
```bash
$ kvwarden bench reproduce-hero --help
```
**Viewer sees:** the reproduce-hero help output showing `--flavor` with choices `2tenant | n6 | n8`, plus the model and duration flags.

### 0:15-0:25 — the published numbers
**Voiceover:** "Gate 2-FAIRNESS on A100: solo baseline fifty-four milliseconds. Vanilla FIFO under load — sixteen-hundred milliseconds, twenty-nine-times starvation. Same workload with kvwarden's admission gate — sixty-one-point-five milliseconds, one-point-one-four times solo."
**Viewer sees:** cut to an overlay (static image, not terminal) showing the published Gate 2-FAIRNESS comparison chart from the LP. Hold for 10 seconds. This is the hero data the LP already leads with.

### 0:25-0:30 — CTA
**Voiceover:** "kvwarden.org. Repo on github. `pip install kvwarden`."
**Viewer sees:** a static end-card with the logo, `kvwarden.org`, and the pip command in large monospace.

> **Note:** Variant B's hero-chart overlay — use `docs/launch/figures/launch_hero_chart.png` from this repo as the static overlay. Don't screenshot the LP live (URL bar leaks, font rendering varies).

---

## Post-production (both variants)

- **Captions.** Social autoplay is muted by default. Burn in the voiceover as captions or use a clean sans-serif overlay (Inter, 24pt, white with 40% black drop shadow). 30s of captions ≈ 75 words — both scripts fit.
- **End card:** logo + kvwarden.org + `pip install kvwarden` in large text. 2 seconds static.
- **Output formats:** 1:1 square (1080x1080) for Twitter/LinkedIn autoplay, 16:9 (1920x1080) for YouTube / Show HN embeds. Same source take, two crops.
- **File size:** under 10 MB for the square cut so Twitter plays it natively. H.264, 30fps, CRF 23 is the sweet spot.

## Which variant to record

- **Default: Variant A.** If an A100 is reachable and the engine stays up for 6 minutes, the real-run-edited-to-30s is more persuasive. Time-lapse is a standard editing move — not a gimmick.
- **Fallback: Variant B.** When GPU availability is uncertain or the filming window is tight, the CLI walkthrough is honest and filmable on a laptop. Note "hero chart from Gate 2-FAIRNESS, reference v3" in the Show HN caption so viewers know the numbers come from a published measurement, not a live take.
- **Don't mix.** Pick one take start-to-finish. Cutting between Variant A footage and Variant B overlay looks sloppy.

## Known CLI surface the script depends on

Cross-checked against `src/kvwarden/cli.py` and `src/kvwarden/_bench/hero.py` on 2026-04-22:
- `kvwarden serve --config <yaml>` — exists (`cli.py:77`).
- `kvwarden bench reproduce-hero --flavor {2tenant|n6|n8}` — exists (`cli.py:183`, flavors defined at `_bench/hero.py:59`).
- Progress bar format: Rich progress with "bench flavor=2-tenant" task and `{completed}/{total}s` text column (`_bench/hero.py:247`).
- Final comparison table: rendered by `_bench/compare.py:create_hero_comparison_table`.
- `kvwarden --help` top-level commands: `serve`, `status`, `models`, `doctor`, `bench`, `man`, `telemetry` (`cli.py` sub-parser list).

Commands deliberately **not** used in this script because they don't exist: `kvwarden bench run`, `kvwarden config set`, `kvwarden reload`, per-tenant Prometheus metric `kvwarden_p99_ttft_ms`. Earlier drafts referenced these; the fix landed alongside this doc.
