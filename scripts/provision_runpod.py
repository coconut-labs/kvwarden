#!/usr/bin/env python3
"""
KVWarden — Automated RunPod A100 Provisioning & Profiling Launcher

Provisions an A100 80GB pod on RunPod, clones the kvwarden repo,
sets up the environment, and launches baseline profiling.

Usage:
    export RUNPOD_API_KEY="your_key"
    export HF_TOKEN="hf_..."
    python3 scripts/provision_runpod.py

    # Or with a specific GPU type:
    python3 scripts/provision_runpod.py --gpu "NVIDIA A100 80GB PCIe"
"""

import argparse
import os
import sys
import time
import json

try:
    import runpod
except ImportError:
    print("ERROR: runpod not installed. Run: pip install runpod")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Provision RunPod for KVWarden profiling")
    parser.add_argument("--gpu", default="NVIDIA A100 80GB PCIe",
                        help="GPU type to request (default: NVIDIA A100 80GB PCIe)")
    parser.add_argument("--cloud-type", default="SECURE", choices=["SECURE", "COMMUNITY", "ALL"],
                        help="Cloud type (default: SECURE)")
    parser.add_argument("--image", default="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
                        help="Docker image")
    parser.add_argument("--disk", type=int, default=100, help="Disk size in GB (default: 100)")
    parser.add_argument("--volume", type=int, default=50, help="Volume size in GB (default: 50)")
    parser.add_argument("--repo-url", default="https://github.com/coconut-labs/kvwarden.git",
                        help="Git repo URL to clone")
    parser.add_argument("--branch", default="main", help="Git branch to clone")
    parser.add_argument("--model-config", default="configs/models/llama31_8b.yaml",
                        help="Model config file path in repo")
    parser.add_argument("--dry-run", action="store_true", help="Print config without creating pod")
    args = parser.parse_args()

    # Validate env vars
    api_key = os.environ.get("RUNPOD_API_KEY")
    hf_token = os.environ.get("HF_TOKEN")

    if not api_key:
        print("ERROR: RUNPOD_API_KEY environment variable not set.")
        print("Get your key at: https://runpod.io/console/user/settings")
        sys.exit(1)

    if not hf_token:
        print("ERROR: HF_TOKEN environment variable not set.")
        print("Get your token at: https://huggingface.co/settings/tokens")
        sys.exit(1)

    runpod.api_key = api_key

    # Build the startup script that will run inside the pod
    startup_script = f"""#!/bin/bash
set -euo pipefail

echo "=== KVWarden Profiling Setup ==="
echo "Started at: $(date -u)"

# Clone repo
cd /workspace
git clone --branch {args.branch} {args.repo_url} kvwarden
cd kvwarden

# Set HF token
export HF_TOKEN="{hf_token}"

# Setup venv and environment
source scripts/setup_venv.sh
bash scripts/setup_gpu_env.sh --model-config {args.model_config}

# Run all baselines (with reduced repeats for speed)
nohup bash scripts/run_all_baselines.sh \\
    --model-config {args.model_config} \\
    --repeats 2 \\
    > /workspace/profiling_run.log 2>&1 &

echo "=== Profiling launched in background ==="
echo "Monitor with: tail -f /workspace/profiling_run.log"
echo "Results will be in /workspace/kvwarden/results_*/"
"""

    pod_config = {
        "name": "kvwarden-profiling",
        "image_name": args.image,
        "gpu_type_id": args.gpu,
        "cloud_type": args.cloud_type,
        "container_disk_in_gb": args.disk,
        "volume_in_gb": args.volume,
        "gpu_count": 1,
        "docker_args": "",
        "env": {
            "HF_TOKEN": hf_token,
            "JUPYTER_PASSWORD": "kvwarden",
        },
    }

    print("\n=== KVWarden RunPod Provisioner ===")
    print(f"GPU:     {args.gpu}")
    print(f"Image:   {args.image}")
    print(f"Disk:    {args.disk}GB + {args.volume}GB volume")
    print(f"Repo:    {args.repo_url} ({args.branch})")
    print(f"Config:  {args.model_config}")

    if args.dry_run:
        print("\n[DRY RUN] Would create pod with config:")
        print(json.dumps(pod_config, indent=2))
        print("\nStartup script:")
        print(startup_script)
        return

    print("\nCreating pod...")
    try:
        pod = runpod.create_pod(**pod_config)
        pod_id = pod.get("id", "unknown")
        print(f"\nPod created: {pod_id}")
        print(f"Dashboard:  https://runpod.io/console/pods/{pod_id}")

        # Wait for pod to be ready
        print("\nWaiting for pod to be ready...")
        for i in range(120):
            status = runpod.get_pod(pod_id)
            pod_status = status.get("desiredStatus", "unknown")
            runtime = status.get("runtime", {})
            if runtime and runtime.get("uptimeInSeconds", 0) > 0:
                ssh_info = runtime.get("ports", [])
                print(f"\nPod is RUNNING (uptime: {runtime['uptimeInSeconds']}s)")
                if ssh_info:
                    for port in ssh_info:
                        if port.get("privatePort") == 22:
                            print(f"SSH: ssh root@{port.get('ip')} -p {port.get('publicPort')}")
                print(f"\nConnect and run:")
                print(f"  # The startup script should be running. Check progress:")
                print(f"  tail -f /workspace/profiling_run.log")
                print(f"\n  # Or run manually:")
                print(f"  cd /workspace/kvwarden")
                print(f"  export HF_TOKEN='{hf_token[:8]}...'")
                print(f"  source scripts/setup_venv.sh")
                print(f"  bash scripts/setup_gpu_env.sh --model-config {args.model_config}")
                print(f"  bash scripts/run_all_baselines.sh --model-config {args.model_config} --repeats 2")
                break
            if i % 10 == 0:
                print(f"  Status: {pod_status} ({i}s elapsed)")
            time.sleep(1)
        else:
            print("\nPod creation timed out. Check dashboard manually.")
            print(f"Dashboard: https://runpod.io/console/pods/{pod_id}")

    except Exception as e:
        print(f"\nERROR creating pod: {e}")
        print("\nCommon issues:")
        print("  - Invalid API key: check RUNPOD_API_KEY")
        print("  - GPU unavailable: try --gpu 'NVIDIA A100-SXM4-80GB' or --cloud-type ALL")
        print("  - Insufficient funds: add credits at runpod.io/console/user/billing")
        sys.exit(1)


if __name__ == "__main__":
    main()
