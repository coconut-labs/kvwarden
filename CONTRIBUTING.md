# Contributing to InferGrid

Thank you for your interest in contributing to InferGrid! This document provides guidelines for contributing.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/coconut-labs/infergrid.git
cd infergrid

# Install with dev dependencies
pip install -e ".[dev,profiling]"

# Run tests
pytest tests/unit/ -v
pytest tests/integration/ -v
```

## Architecture Overview

InferGrid is a middleware orchestration layer sitting on top of vLLM and SGLang:

```
    WorkloadRouter  (request profiling, SLO-aware routing)
         |
    CacheManager    (GPU -> CPU -> SSD KV cache tiering)
         |
    TenantManager   (per-tenant resource budgets)
         |
    vLLM / SGLang   (inference engines)
```

### Key directories

- `src/infergrid/router/` — WorkloadRouter: request routing, model lifecycle, priority scheduling
- `src/infergrid/cache/` — CacheManager: KV cache tiering across GPU/CPU/SSD
- `src/infergrid/tenant/` — TenantManager: multi-tenant isolation and resource budgets
- `src/infergrid/engines/` — Adapters for vLLM and SGLang backends
- `profiling/` — Scheduling overhead profiling scripts
- `benchmarks/` — Head-to-head comparison benchmarks
- `scripts/` — Cloud provisioning and GPU setup automation

## Running Profiling on Cloud GPUs

```bash
# On a RunPod/Lambda Labs instance with NVIDIA GPU:
export HF_TOKEN="your_token"
export ENGINE="vllm"  # or "sglang"
export GPU_LABEL="a100-sxm"
bash scripts/cloud_benchmark.sh
```

See `scripts/cloud_benchmark.sh` for full options.

## Pull Request Process

1. Fork the repo and create a feature branch from `main`
2. Write tests for new functionality
3. Ensure all tests pass: `pytest tests/ -v`
4. Update documentation if needed
5. Submit a PR with a clear description of changes

### PR guidelines

- Keep PRs focused — one feature or fix per PR
- Include benchmark results if your change affects performance
- Add type hints to all public functions
- Follow existing code style (no linter enforced, just be consistent)

## Reporting Issues

Use the issue templates for bug reports and feature requests. Include:
- GPU model and driver version
- Python version and OS
- vLLM/SGLang version
- Reproduction steps and error logs

## Code Style

- Python 3.11+
- Type hints on public APIs
- Async throughout (asyncio + aiohttp)
- No unnecessary abstractions — working code over elegant patterns

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
