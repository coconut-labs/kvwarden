# Changelog

All notable changes to the [`kvwarden`](https://pypi.org/project/kvwarden/) package are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned for v0.2.0 (mid-June 2026)

- **Cache-pressure-aware admission.** kvwarden polls vLLM's `vllm:kv_cache_usage_perc` gauge (250 ms cadence) and scales admission priority by engine cache load. Same budget gate, smarter gating. Honest scope: 0.2 reacts to *global* cache pressure, not per-tenant pressure (the gauge has no tenant label). Per-tenant cache visibility waits on the LMCache substrate in 0.3+. RFC: [`docs/rfcs/T2-cache-pressure-admission.md`](docs/rfcs/T2-cache-pressure-admission.md). Tracker: [#103](https://github.com/coconut-labs/kvwarden/issues/103). Gated on the M4 Path C measure-first probe (2026-05-13 → 2026-05-19).

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

[Unreleased]: https://github.com/coconut-labs/kvwarden/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/coconut-labs/kvwarden/releases/tag/v0.1.5
[0.1.4]: https://github.com/coconut-labs/kvwarden/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/coconut-labs/kvwarden/compare/v0.0.1...v0.1.3
[0.0.1]: https://pypi.org/project/kvwarden/0.0.1/
