#!/usr/bin/env python3
"""Generate benchmark comparison charts from profiling data."""

import json
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'results')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'docs', 'figures')
os.makedirs(OUTPUT_DIR, exist_ok=True)

DATASETS = {
    'vLLM A100 SXM': 'results_vllm_a100-sxm_20260416_182457/profiling/external/summary.json',
    'SGLang A100 SXM': 'results_sglang_a100-sxm_20260416_183302/profiling/external/summary.json',
    'vLLM H100 SXM': 'results_vllm_h100-sxm_20260416_182420/profiling/external/summary.json',
}

COLORS = {
    'vLLM A100 SXM': '#2196F3',
    'SGLang A100 SXM': '#FF9800',
    'vLLM H100 SXM': '#4CAF50',
}

def load_data():
    data = {}
    for name, rel_path in DATASETS.items():
        path = os.path.join(RESULTS_DIR, rel_path)
        if os.path.exists(path):
            with open(path) as f:
                data[name] = json.load(f)
    return data


def plot_throughput(data):
    fig, ax = plt.subplots(figsize=(10, 6))
    for name, d in data.items():
        concs = sorted(d.keys(), key=int)
        x = [int(c) for c in concs]
        y = [d[c]['throughput_tok_per_sec'] for c in concs]
        ax.plot(x, y, 'o-', label=name, color=COLORS[name], linewidth=2, markersize=8)

    ax.set_xlabel('Concurrency', fontsize=13)
    ax.set_ylabel('Throughput (tok/s)', fontsize=13)
    ax.set_title('Throughput vs Concurrency — The Scheduling Cliff', fontsize=15, fontweight='bold')
    ax.set_xscale('log', base=2)
    ax.set_xticks([1, 8, 32, 64, 128, 256])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3)

    # Annotate the cliff
    ax.axvspan(128, 256, alpha=0.1, color='red')
    ax.annotate('Scheduling\nCliff', xy=(180, 3000), fontsize=11, color='red', fontweight='bold')

    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, 'throughput_vs_concurrency.png'), dpi=150)
    print(f'Saved: throughput_vs_concurrency.png')
    plt.close()


def plot_ttft(data):
    fig, ax = plt.subplots(figsize=(10, 6))
    for name, d in data.items():
        concs = sorted(d.keys(), key=int)
        x = [int(c) for c in concs]
        y = [d[c]['ttft_p50_ms'] for c in concs]
        ax.plot(x, y, 'o-', label=name, color=COLORS[name], linewidth=2, markersize=8)

    ax.set_xlabel('Concurrency', fontsize=13)
    ax.set_ylabel('TTFT p50 (ms)', fontsize=13)
    ax.set_title('Time to First Token vs Concurrency', fontsize=15, fontweight='bold')
    ax.set_xscale('log', base=2)
    ax.set_yscale('log')
    ax.set_xticks([1, 8, 32, 64, 128, 256])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3)

    # Annotate SGLang advantage
    ax.annotate('SGLang 2.2x\nbetter TTFT', xy=(256, 1100), fontsize=10, color=COLORS['SGLang A100 SXM'], fontweight='bold')

    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, 'ttft_vs_concurrency.png'), dpi=150)
    print(f'Saved: ttft_vs_concurrency.png')
    plt.close()


def plot_tpot(data):
    fig, ax = plt.subplots(figsize=(10, 6))
    for name, d in data.items():
        concs = sorted(d.keys(), key=int)
        x = [int(c) for c in concs]
        y = [d[c]['tpot_p50_ms'] for c in concs]
        ax.plot(x, y, 'o-', label=name, color=COLORS[name], linewidth=2, markersize=8)

    ax.set_xlabel('Concurrency', fontsize=13)
    ax.set_ylabel('TPOT p50 (ms/token)', fontsize=13)
    ax.set_title('Time Per Output Token — Stable Across Concurrency', fontsize=15, fontweight='bold')
    ax.set_xscale('log', base=2)
    ax.set_xticks([1, 8, 32, 64, 128, 256])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, 'tpot_vs_concurrency.png'), dpi=150)
    print(f'Saved: tpot_vs_concurrency.png')
    plt.close()


def plot_scheduling_cliff_detail(data):
    """Side-by-side: throughput gain vs TTFT cost at the cliff."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    configs = ['vLLM A100 SXM', 'SGLang A100 SXM', 'vLLM H100 SXM']
    colors = [COLORS[c] for c in configs]

    # Throughput gain c128->c256
    gains = []
    ttft_ratios = []
    for name in configs:
        d = data[name]
        t128 = d['128']['throughput_tok_per_sec']
        t256 = d['256']['throughput_tok_per_sec']
        gains.append((t256 / t128 - 1) * 100)

        f128 = d['128']['ttft_p50_ms']
        f256 = d['256']['ttft_p50_ms']
        ttft_ratios.append(f256 / f128)

    x = np.arange(len(configs))
    bars1 = ax1.bar(x, gains, color=colors, alpha=0.8)
    ax1.set_ylabel('Throughput Gain (%)', fontsize=12)
    ax1.set_title('c=128 → c=256: Throughput', fontsize=13, fontweight='bold')
    ax1.set_xticks(x)
    ax1.set_xticklabels([c.replace(' SXM', '') for c in configs], fontsize=10)
    ax1.axhline(y=0, color='black', linewidth=0.5)
    for bar, val in zip(bars1, gains):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1, f'{val:+.1f}%', ha='center', fontsize=11, fontweight='bold')

    bars2 = ax2.bar(x, ttft_ratios, color=colors, alpha=0.8)
    ax2.set_ylabel('TTFT Increase (x)', fontsize=12)
    ax2.set_title('c=128 → c=256: TTFT Cost', fontsize=13, fontweight='bold')
    ax2.set_xticks(x)
    ax2.set_xticklabels([c.replace(' SXM', '') for c in configs], fontsize=10)
    for bar, val in zip(bars2, ttft_ratios):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1, f'{val:.1f}x', ha='center', fontsize=11, fontweight='bold')

    fig.suptitle('The Scheduling Cliff: Marginal Throughput at Massive TTFT Cost', fontsize=15, fontweight='bold', y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, 'scheduling_cliff_detail.png'), dpi=150, bbox_inches='tight')
    print(f'Saved: scheduling_cliff_detail.png')
    plt.close()


def plot_engine_convergence(data):
    """vLLM vs SGLang throughput gap — showing convergence."""
    fig, ax = plt.subplots(figsize=(10, 6))

    vllm = data['vLLM A100 SXM']
    sglang = data['SGLang A100 SXM']
    concs = sorted(vllm.keys(), key=int)
    x = [int(c) for c in concs]

    gaps = []
    for c in concs:
        v = vllm[c]['throughput_tok_per_sec']
        s = sglang[c]['throughput_tok_per_sec']
        gap = (v - s) / s * 100
        gaps.append(gap)

    bars = ax.bar(x, gaps, color=['#2196F3' if g > 0 else '#FF9800' for g in gaps], alpha=0.8, width=[0.8, 6, 25, 50, 100, 200])
    ax.set_xlabel('Concurrency', fontsize=13)
    ax.set_ylabel('vLLM vs SGLang Gap (%)', fontsize=13)
    ax.set_title('Engine Convergence: vLLM vs SGLang Throughput Gap (A100 SXM)', fontsize=14, fontweight='bold')
    ax.axhline(y=0, color='black', linewidth=1)
    ax.axhspan(-5, 5, alpha=0.1, color='green')
    ax.annotate('Within 5% = parity', xy=(32, 4), fontsize=10, color='green')
    ax.set_xscale('log', base=2)
    ax.set_xticks([1, 8, 32, 64, 128, 256])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.grid(True, alpha=0.3, axis='y')

    for i, (xi, g) in enumerate(zip(x, gaps)):
        ax.text(xi, g + (0.3 if g > 0 else -0.8), f'{g:+.1f}%', ha='center', fontsize=10, fontweight='bold')

    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, 'engine_convergence.png'), dpi=150)
    print(f'Saved: engine_convergence.png')
    plt.close()


if __name__ == '__main__':
    data = load_data()
    print(f'Loaded {len(data)} datasets')

    plot_throughput(data)
    plot_ttft(data)
    plot_tpot(data)
    plot_scheduling_cliff_detail(data)
    plot_engine_convergence(data)

    print(f'\nAll charts saved to {OUTPUT_DIR}/')
