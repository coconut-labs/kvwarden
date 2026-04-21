#!/usr/bin/env python3
"""Post-run summary generator for KVWarden profiling results.

Reads JSON/CSV results from profiling and benchmark runs, then produces
a markdown summary with key numbers for docs/phase1_findings.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json_safe(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  Warning: Could not read {path}: {exc}", file=sys.stderr)
        return None


def find_summary_data(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Scan results directory and collect all summary data by model name."""
    models_data: dict[str, dict[str, Any]] = {}
    
    meta_files = list(results_dir.glob("**/run_metadata.json"))
    if not meta_files and (results_dir / "run_metadata.json").exists():
        meta_files = [results_dir / "run_metadata.json"]
        
    for meta_path in meta_files:
        run_dir = meta_path.parent
        meta = load_json_safe(meta_path)
        if not isinstance(meta, dict): continue
        
        model_id = meta.get("model", "unknown")
        model_name = model_id.split("/")[-1]
        
        if model_name not in models_data:
            models_data[model_name] = {
                "vllm_summaries": {},
                "sglang_summaries": {},
                "comparisons": [],
                "run_metadata": meta,
                "gpu_metrics_files": [],
            }
        
        data = models_data[model_name]
        data["run_metadata"] = meta
        
        for engine in ["vllm", "sglang"]:
            key = f"{engine}_summaries"
            sp = run_dir / "profiling" / engine / "summary.json"
            if not sp.exists():
                sp = run_dir / f"profiling/results/{engine}/external/summary.json"
            if sp.exists():
                loaded = load_json_safe(sp)
                if isinstance(loaded, dict):
                    if "throughput_tok_per_sec" in loaded:
                        conc = loaded.get("concurrency", "unknown")
                        data[key][str(conc)] = loaded
                    else:
                        data[key].update(loaded)
                    
        comp_files = list(run_dir.glob("**/comparison_summary.json"))
        if not comp_files:
            comp_files = list(run_dir.glob("**/comparison.json"))
        for cf in comp_files:
            loaded = load_json_safe(cf)
            if isinstance(loaded, list):
                data["comparisons"].extend(loaded)
            elif isinstance(loaded, dict):
                data["comparisons"].append(loaded)
                
        data["gpu_metrics_files"].extend(sorted(run_dir.glob("**/*gpu*.csv")))
        
    return models_data


def format_number(val: Any, fmt: str = ".1f") -> str:
    if val is None or val == 0:
        return "—"
    try:
        return f"{float(val):{fmt}}"
    except (ValueError, TypeError):
        return str(val)


def generate_metadata_section(model_name: str, metadata: dict[str, Any] | None) -> str:
    lines = [f"## Model: {model_name}", ""]
    if metadata:
        lines.extend([
            f"- **Hardware:** {metadata.get('gpu', 'n/a')}",
            f"- **Driver:** {metadata.get('driver', 'n/a')}",
            f"- **Model ID:** {metadata.get('model', 'n/a')}",
            f"- **Workload:** {metadata.get('workload', 'n/a')}",
            f"- **Concurrency sweep:** {metadata.get('concurrency', 'n/a')}",
            f"- **Requests per level:** {metadata.get('num_requests', 'n/a')}",
            f"- **Timestamp:** {metadata.get('timestamp', 'n/a')}",
        ])
    lines.extend([
        "",
        "### Tools",
        "- py-spy: flame graph generation (SVG + speedscope)",
        "- pynvml: GPU monitoring",
        "- Custom async benchmark client",
        "",
    ])
    return "\n".join(lines)


def generate_throughput_table(vllm_data: dict[str, Any], sglang_data: dict[str, Any]) -> str:
    lines = [
        "### Throughput Comparison (tokens/second)", "",
        "| Concurrency | vLLM | SGLang | Gap (%) |",
        "|-------------|------|--------|---------|"
    ]
    all_concs = sorted(set(list(vllm_data.keys()) + list(sglang_data.keys())), key=lambda x: int(x) if x.isdigit() else 0)
    for conc in all_concs:
        v_tp = vllm_data.get(conc, {}).get("throughput_tok_per_sec")
        s_tp = sglang_data.get(conc, {}).get("throughput_tok_per_sec")
        gap = ""
        if v_tp and s_tp and s_tp > 0:
            gap = f"{((s_tp - v_tp) / s_tp) * 100:+.1f}%"
        lines.append(f"| {conc} | {format_number(v_tp)} | {format_number(s_tp)} | {gap or '—'} |")
    if not all_concs:
        lines.append("| — | No data | — | — |")
    lines.append("")
    return "\n".join(lines)


def generate_latency_table(vllm_data: dict[str, Any], sglang_data: dict[str, Any]) -> str:
    lines = [
        "### Latency Comparison (TTFT p50/p99, ms)", "",
        "| Concurrency | vLLM TTFT p50 | vLLM TTFT p99 | SGLang TTFT p50 | SGLang TTFT p99 |",
        "|-------------|---------------|---------------|-----------------|-----------------|"
    ]
    all_concs = sorted(set(list(vllm_data.keys()) + list(sglang_data.keys())), key=lambda x: int(x) if x.isdigit() else 0)
    for conc in all_concs:
        v = vllm_data.get(conc, {})
        s = sglang_data.get(conc, {})
        lines.append(
            f"| {conc} | {format_number(v.get('ttft_p50_ms'))} | {format_number(v.get('ttft_p99_ms'))} | "
            f"{format_number(s.get('ttft_p50_ms'))} | {format_number(s.get('ttft_p99_ms'))} |"
        )
    if not all_concs:
        lines.append("| — | No data | — | — | — |")
    lines.extend([
        "", "#### TPOT Comparison (ms)", "",
        "| Concurrency | vLLM TPOT p50 | SGLang TPOT p50 |",
        "|-------------|---------------|-----------------|"
    ])
    for conc in all_concs:
        v = vllm_data.get(conc, {})
        s = sglang_data.get(conc, {})
        lines.append(f"| {conc} | {format_number(v.get('tpot_p50_ms'))} | {format_number(s.get('tpot_p50_ms'))} |")
    if not all_concs:
        lines.append("| — | No data | — |")
    lines.append("")
    return "\n".join(lines)


def generate_gpu_util_table(vllm_data: dict[str, Any], sglang_data: dict[str, Any]) -> str:
    lines = [
        "### GPU Utilization Patterns", "",
        "| Concurrency | vLLM Util % | SGLang Util % | Delta |",
        "|-------------|-------------|---------------|-------|"
    ]
    all_concs = sorted(set(list(vllm_data.keys()) + list(sglang_data.keys())), key=lambda x: int(x) if x.isdigit() else 0)
    for conc in all_concs:
        v = vllm_data.get(conc, {}).get("gpu_utilization_mean")
        s = sglang_data.get(conc, {}).get("gpu_utilization_mean")
        delta = f"{s - v:+.1f}" if v is not None and s is not None else ""
        lines.append(f"| {conc} | {format_number(v)} | {format_number(s)} | {delta or '—'} |")
    if not all_concs:
        lines.append("| — | No data | — | — |")
    lines.append("")
    return "\n".join(lines)


def generate_cross_model_comparison(models_data: dict[str, dict[str, Any]]) -> str:
    lines = [
        "## Cross-Model Comparison", "",
        "Shows throughput (tok/s) across models and engines for identical concurrency levels.", "",
        "| Concurrency | Model | vLLM (tok/s) | SGLang (tok/s) |",
        "|-------------|-------|--------------|----------------|"
    ]
    all_concs = set()
    for m, d in models_data.items():
        all_concs.update(d["vllm_summaries"].keys())
        all_concs.update(d["sglang_summaries"].keys())
        
    for conc in sorted(all_concs, key=lambda x: int(x) if x.isdigit() else 0):
        for m, d in models_data.items():
            v_tp = d["vllm_summaries"].get(conc, {}).get("throughput_tok_per_sec")
            s_tp = d["sglang_summaries"].get(conc, {}).get("throughput_tok_per_sec")
            if v_tp or s_tp:
                lines.append(f"| {conc} | {m} | {format_number(v_tp)} | {format_number(s_tp)} |")
                
    lines.append("")
    return "\n".join(lines)


def generate_full_summary(data: dict[str, dict[str, Any]]) -> str:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [
        f"# Phase 1 Findings: Scheduling Overhead", "",
        f"*Generated: {timestamp}*", "",
        "This document presents Phase 1 profiling results measuring scheduling overhead ",
        "in vLLM and SGLang across multiple models including dense and MoE architectures.",
        ""
    ]
    
    if not data:
        sections.append("> **No profiling data found.** Run `bash scripts/run_all_baselines.sh`.")
        return "\n".join(sections)
        
    for model_name, mdata in data.items():
        sections.append(generate_metadata_section(model_name, mdata["run_metadata"]))
        sections.append(generate_throughput_table(mdata["vllm_summaries"], mdata["sglang_summaries"]))
        sections.append(generate_latency_table(mdata["vllm_summaries"], mdata["sglang_summaries"]))
        sections.append(generate_gpu_util_table(mdata["vllm_summaries"], mdata["sglang_summaries"]))
        
    sections.append(generate_cross_model_comparison(data))
    
    # Intervention points
    sections.extend([
        "## Identified Intervention Points for WorkloadRouter", "",
        "### Priority 1: Batch Construction Optimization",
        "- **Problem:** Construction padding waste.",
        "- **Intervention:** Pre-sort requests by estimated length.", "",
        "### Priority 2: KV Cache Pre-allocation",
        "- **Intervention:** Predict KV cache requirements at routing time.", "",
        "### Priority 3: Asynchronous Scheduling Pipeline",
        "- **Intervention:** Pipeline scheduling with GPU execution.", ""
    ])

    return "\n".join(sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--results-dir", type=str, default=".", help="Root directory containing results")
    parser.add_argument("--output", type=str, default="docs/phase1_findings.md", help="Output file path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir)
    output_path = Path(args.output)

    print(f"Scanning results in: {results_dir}")
    data = find_summary_data(results_dir)

    has_any = len(data) > 0
    if has_any:
        for m, d in data.items():
            print(f"  {m} - vLLM: {len(d['vllm_summaries'])}, SGLang: {len(d['sglang_summaries'])}, H2H: {len(d['comparisons'])}")
    else:
        print("  No profiling data found — generating placeholder summary")

    summary = generate_full_summary(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(summary)

    print(f"Summary written to: {output_path}")


if __name__ == "__main__":
    main()
