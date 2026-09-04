# Changelog

All notable changes to the [`kvwarden`](https://pypi.org/project/kvwarden/) package are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Cache-pressure admission, shadow mode.** New `cache_pressure_admission` config block, disabled by default. Enabled, it polls each loaded engine's `/metrics` for `vllm:kv_cache_usage_perc`, surfaces the reading at `/status` under `cache.kv_cache_pressure`, and records what the pressure lever *would* do to each request's priority — `kvwarden_kv_cache_pressure`, `kvwarden_cache_pressure_scale`, `kvwarden_shadow_priority_delta`, `kvwarden_cache_pressure_poll_failures_total`. The priority handed to the admission gate is unchanged; enforcement is a later slice, and should not land before a probe replaces the curve's placeholder watermarks with measured ones. Sample config: [`configs/t2_cache_pressure_shadow.yaml`](configs/t2_cache_pressure_shadow.yaml). Refs [#103](https://github.com/coconut-labs/kvwarden/issues/103).

### Planned for v0.2.0 (mid-June 2026)

- **Cache-pressure-aware admission.** kvwarden polls vLLM's `vllm:kv_cache_usage_perc` gauge (250 ms cadence) and scales admission priority by engine cache load. Same budget gate, smarter gating. Honest scope: 0.2 reacts to *global* cache pressure, not per-tenant pressure (the gauge has no tenant label). Per-tenant cache visibility waits on the LMCache substrate in 0.3+. RFC: [`docs/rfcs/T2-cache-pressure-admission.md`](docs/rfcs/T2-cache-pressure-admission.md). Tracker: [#103](https://github.com/coconut-labs/kvwarden/issues/103). Gated on the M4 Path C measure-first probe (2026-05-13 → 2026-05-19).

## [0.1.6] — 2026-08-08 — dependency hygiene

Maintenance release. No behaviour change to the router, the admission gate, or the tenant budget — every published number still stands and no bench was re-run. What changed is the dependency surface and the CI gates around it.

### Removed

- **`numpy`, `pandas` and `httpx` dropped from runtime dependencies.** None of the three is imported anywhere under `src/kvwarden/`; they were only ever needed by the bench harness (`benchmarks/`) and the pod orchestrators (`scripts/`), neither of which ships in the wheel. Verified against the built wheel — its only top-level entry is `kvwarden/`.

  Measured effect on a clean py3.13 install: **144 MB / 27 packages → 33 MB / 18 packages.**

  They are still one flag away — `pip install kvwarden[bench]` restores all three.

  **Potentially breaking** if you relied on `pip install kvwarden` to pull numpy/pandas/httpx into your environment as a side effect. Add the `bench` extra, or declare them yourself.

- **`black` dropped from the `dev` extra.** It was never wired into CI — `ruff format` is the formatting gate — and 23.12.1 carried three advisories (PYSEC-2024-48, PYSEC-2026-2120, PYSEC-2026-2121).

### Fixed

- **`pytest` floor raised to `>=8.0`.** The `dev` extra pinned `~=7.0`, resolving to 7.4.4, which carries PYSEC-2026-1845 — and `requirements-gpu.txt` had asked for `>=8.0` since the vLLM 0.19.1 bump. CI installs from the `dev` extra, so CI was running the vulnerable one. Suite verified green on pytest 9.1.1.

- **Three pyproject ↔ requirements-gpu.txt contradictions closed.** The GPU requirements file documented the `numpy<2.3` ceiling and the `transformers<5.0` ceiling as lifted when the tree moved to vLLM 0.19.1, but `pyproject.toml` still carried both, plus the conflicting pytest floor above. `pyproject.toml` now agrees: no numpy ceiling, `transformers>=4.51.1,<6.0`, `pytest>=8.0`.

- **`pip install -e ".[dev]"` no longer collects errors.** `dev` now pulls `kvwarden[bench]`, because `tests/unit/test_profiling_utils.py` and `tests/integration/test_benchmark_client.py` import numpy and pandas directly. Without it, moving those two out of runtime deps would have broken collection on a fresh clone.

### Changed

- **`pytest-asyncio` floor raised to `>=1.0,<2.0`** and `asyncio_default_fixture_loop_scope = "function"` set explicitly, so the fixture loop scope stops being a moving target across minors. Verified on 1.4.0.

- **`ruff` constrained to `>=0.16,<0.17`** in both the `dev` extra and the CI lint job. The old `~=0.1` admitted any minor below 1.0, so a new default rule set could turn CI red with no change on our side. Rule selection is unchanged.

- **`mypy` floor raised to `>=1.11,<3.0`.** Still not a CI gate; the bump is hygiene only.

### Added

- **`bench` extra** — `numpy`, `pandas`, `httpx`. What the benchmark harness and the RunPod orchestrators need, separated from what the router needs.

- **`audit` CI job** running `pip-audit` against the installed `dev` tree, on every push and PR. The four advisories above sat in the dev extra for months with nothing watching for them.

### Unchanged, deliberately

- **`vllm==0.19.1` stays hard-pinned** (latest is 0.26.0). Every published number — the 53.9 / 1,585 / 61.5 ms hero, the N=6 generalization — is keyed to 0.19.1. Widening this without a GPU re-bench would quietly detach the claims from the pin they were measured on. Tracked separately.
- **`sglang~=0.3.0` stays** (latest is 0.5.17) for the same reason, and because the adapter almost certainly needs work to follow.
- **Python 3.14 is not in the CI matrix.** Adding a leg nobody has watched pass is not a gate.

### Verification

Full suite on py3.13: **281 passed, 4 skipped, 10 xfailed**. `ruff check` and `ruff format --check` clean over 48 files. `pip-audit` clean on both the runtime-only and the `dev` trees (only the venv's own bundled `pip` is flagged, which CI upgrades before installing).

## [0.1.5] — 2026-05-12 — Show HN launch tag

Docs-only release. No Python source changes since v0.1.4; this tag exists so PyPI, the GitHub release page, the README pin, the FAQ pin, and the Show HN post draft all reference the same version on launch day.

### Changed

- **README "About the name"** — v0.2 framing reset from "tenant-aware KV eviction" to "cache-pressure-aware admission." The 04-28 reframe (eviction → admission, locked after verifying the cache-manager scaffold is a shadow ledger no engine adapter reads) now propagates to the launch surface. The original eviction RFC (#116) is closed as superseded by RFC #121.
- **README + FAQ** — global-gauge limitation surfaced upfront: `vllm:kv_cache_usage_perc` is labeled by `model_name` only, not by tenant. v0.2 reacts to global cache pressure, not whose. Per-tenant cache visibility waits on LMCache (v0.3+).
- **FAQ** — new entry: *"The name says KV warden but you're not touching the KV cache."*
- **`docs/launch/show_hn.md`** — hero pin to v0.1.5, test count to ~200, total compute to ~$24 (drift since the original draft from Gate 2.1b $1.64 + M4 aborted attempt $5.33).

### Released

- [PyPI v0.1.5](https://pypi.org/project/kvwarden/0.1.5/) — `pip install kvwarden==0.1.5`
- [GitHub release v0.1.5](https://github.com/coconut-labs/kvwarden/releases/tag/v0.1.5)

## [0.1.4] — 2026-04-22 — PyPI hero image fix

### Fixed

- README hero chart used a relative path in the v0.1.3 PyPI README, which rendered broken on pypi.org. Switched to absolute raw-content URL so the chart loads on the PyPI project page.

## [0.1.3] — 2026-04-22 — First substantive `kvwarden` release on PyPI

The first real release of the package under the `kvwarden` name. v0.0.1 was a placeholder; v0.1.3 is the first version a user installing `pip install kvwarden` can do anything with.

### Added

- Full middleware: tenant-aware admission (token-bucket at the budget gate), multi-model lifecycle on a single GPU (freq+recency eviction, hot-swap), OpenAI-compatible HTTP API in front of vLLM and SGLang subprocess adapters.
- CLI: `kvwarden serve`, `kvwarden bench`, `kvwarden status`, `kvwarden models`, `kvwarden man`, `kvwarden telemetry`, `kvwarden doctor`.
- Opt-in anonymous install/usage telemetry (first interactive run prompts; default off).
- Reproduce-hero one-liner: `kvwarden bench reproduce-hero` against a vLLM 0.19.1 install on A100 + Llama-3.1-8B.

### Note on the rename

This is the first kvwarden release shipped with substantive code; the prior development lineage lived on PyPI as `infergrid` (versions 0.1.1 and 0.1.2). The `infergrid` package now ships a deprecation stub pointing here. Audit trail: [`docs/naming/rename_plan.md`](docs/naming/rename_plan.md), [`docs/naming/rename_sequence.md`](docs/naming/rename_sequence.md), [`docs/naming/infergrid_name_audit.md`](docs/naming/infergrid_name_audit.md).

## [0.0.1] — 2026-04-22 — PyPI name reservation stub

Placeholder release on PyPI to claim the `kvwarden` package name in the window between the rename becoming visible (any tweet, any commit on a public repo) and the real v0.1.3 release. No usable code — a stub `__init__.py` and an import-only `__version__`. Rationale and full reservation runbook: [`docs/launch/pypi_reservation.md`](docs/launch/pypi_reservation.md).

---

## Pre-rename history (package was named `infergrid`)

Prior to the 2026-04-22 rename, the same codebase shipped on PyPI under the [`infergrid`](https://pypi.org/project/infergrid/) name. The `infergrid` 0.1.1 and 0.1.2 releases are not part of `kvwarden`'s version history; they are documented here for continuity only. The current `infergrid` PyPI package is a deprecation stub.

- **infergrid 0.1.2** (2026-04-20) — Interactive `serve` wizard, offline `man [topic]` help, `doctor` env sanity check, rich-table CLI output, durable `__version__` via `importlib.metadata`.
- **infergrid 0.1.1** (2026-04-20) — `pyproject.toml` TOML structure fix that broke `pip install -e .` on some Python builds.

[Unreleased]: https://github.com/coconut-labs/kvwarden/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/coconut-labs/kvwarden/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/coconut-labs/kvwarden/releases/tag/v0.1.5
[0.1.4]: https://github.com/coconut-labs/kvwarden/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/coconut-labs/kvwarden/compare/v0.0.1...v0.1.3
[0.0.1]: https://pypi.org/project/kvwarden/0.0.1/
