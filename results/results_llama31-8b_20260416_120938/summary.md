# Phase 1 Findings: Scheduling Overhead

*Generated: 2026-04-16 14:19 UTC*

This document presents Phase 1 profiling results measuring scheduling overhead
in vLLM and SGLang on NVIDIA A100 80GB PCIe.

## Model: Llama-3.1-8B-Instruct

- **Hardware:** NVIDIA A100 80GB PCIe
- **Driver:** 555.42.02
- **Model ID:** meta-llama/Llama-3.1-8B-Instruct
- **Workload:** Fixed-length synthetic (input=512, output=256)
- **Concurrency sweep:** 1, 8, 32, 64, 128, 256
- **Requests per level:** 200
- **Timestamp:** 2026-04-16T12:09:40Z

### Tools
- py-spy: flame graph generation (SVG + speedscope)
- pynvml: GPU monitoring
- Custom async benchmark client

### Throughput (vLLM, A100 PCIe)

SGLang could not load the model in this environment (version incompatibility).

| Concurrency | vLLM (tok/s) | Std Dev |
|:-----------:|:------------:|:-------:|
| 1 | 86 | 0.3 |
| 8 | 663 | 7.7 |
| 32 | 2,014 | 78.1 |
| 64 | 3,196 | 11.4 |
| 128 | 5,014 | 94.2 |
| 256 | 5,121 | 0.8 |

### Latency (vLLM, A100 PCIe)

| Concurrency | TTFT p50 (ms) | TTFT p99 (ms) | TPOT p50 (ms) |
|:-----------:|:-------------:|:-------------:|:-------------:|
| 1 | 22.3 | 32.6 | 11.5 |
| 8 | 41.2 | 1,044.4 | 11.8 |
| 32 | 67.9 | 1,131.8 | 13.7 |
| 64 | 96.3 | 163.4 | 16.6 |
| 128 | 318.5 | 5,140.9 | 19.1 |
| 256 | 2,608.4 | 5,171.2 | 19.0 |

### GPU Utilization (vLLM, A100 PCIe)

| Concurrency | GPU Util Mean % | GPU Util p50 % |
|:-----------:|:---------------:|:--------------:|
| 1 | 99.3 | 100.0 |
| 8 | 98.6 | 100.0 |
| 32 | 95.4 | 100.0 |
| 64 | 99.3 | 100.0 |
| 128 | 97.4 | 100.0 |
| 256 | 98.5 | 100.0 |

## Cross-GPU Comparison (A100 SXM / H100 SXM)

### Throughput (tok/s)

| Concurrency | vLLM A100 SXM | SGLang A100 SXM | vLLM H100 SXM |
|:-----------:|:-------------:|:---------------:|:--------------:|
| 1 | 90 | 84 | 150 |
| 8 | 690 | 691 | 1,142 |
| 32 | 2,060 | 2,155 | 3,590 |
| 64 | 3,314 | 3,348 | 6,365 |
| 128 | 5,334 | 5,276 | 10,341 |
| 256 | 5,353 | 5,214 | 10,545 |

**Key finding:** vLLM-SGLang throughput gap is <5% across all concurrency levels.

### TTFT at Saturation

| Config | TTFT p50 (ms) |
|--------|:-------------:|
| vLLM A100 SXM c=128 | 150.9 |
| vLLM A100 SXM c=256 | 2,315.2 |
| SGLang A100 SXM c=128 | 102.4 |
| SGLang A100 SXM c=256 | 1,052.8 |
| vLLM H100 SXM c=128 | 184.8 |
| vLLM H100 SXM c=256 | 1,293.4 |

**Key finding:** SGLang has 2.2x better TTFT at saturation (c=256) on A100 SXM.

## Identified Intervention Points for WorkloadRouter

### Priority 1: Admission Control at the Scheduling Cliff
- **Problem:** TTFT degrades 8-15x when concurrency exceeds the saturation point.
- **Intervention:** Intelligent admission control to maintain TTFT SLOs.
- **Expected impact:** 2-4x p99 TTFT improvement at saturation.

### Priority 2: KV Cache Pre-allocation
- **Intervention:** Predict KV cache requirements at routing time.

### Priority 3: Multi-Model Lifecycle Management
- **Intervention:** Frequency+recency model loading/eviction (not naive LRU).
