"""
Phase 1: Profile vLLM scheduling overhead.

Goal: Reproduce WukLab findings that vLLM scheduling takes >50% of inference time.
Measures CPU scheduling time vs GPU execution time per iteration.

Usage:
    python profiling/scripts/profile_vllm_scheduler.py \
        --model meta-llama/Llama-3.1-8B-Instruct \
        --dataset sharegpt \
        --num-requests 100
"""

import argparse
import time
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Profile vLLM scheduling overhead")
    parser.add_argument("--model", type=str, default="meta-llama/Llama-3.1-8B-Instruct")
    parser.add_argument("--dataset", type=str, default="sharegpt", choices=["sharegpt", "synthetic"])
    parser.add_argument("--num-requests", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--output-dir", type=str, default="profiling/results")
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== InferGrid Phase 1: vLLM Scheduler Profiling ===")
    print(f"Model: {args.model}")
    print(f"Dataset: {args.dataset}")
    print(f"Requests: {args.num_requests}")
    print()

    # TODO: Implement profiling
    # Step 1: Start vLLM server with profiling hooks
    # Step 2: Send requests from dataset
    # Step 3: Collect per-iteration timing:
    #   - scheduler_time_ms: time in Python scheduler code
    #   - gpu_forward_time_ms: time in model forward pass
    #   - preprocess_time_ms: time in tensor pre/post-processing
    #   - total_iteration_time_ms: wall clock per iteration
    # Step 4: Compute scheduling_overhead_pct = scheduler_time / total_time
    # Step 5: Save results

    print("TODO: Implement profiling hooks into vLLM scheduler")
    print("See WukLab study for methodology: https://mlsys.wuklab.io/posts/scheduling_overhead/")
    print()
    print("Key measurements needed:")
    print("  1. Per-iteration: scheduler_time vs gpu_forward_time")
    print("  2. Breakdown: tensor preprocessing, batch construction, detokenization")
    print("  3. Scaling: how overhead changes with num_requests (1, 10, 100, 1000)")
    print("  4. Compare: vLLM vs SGLang on identical workload")


if __name__ == "__main__":
    main()
