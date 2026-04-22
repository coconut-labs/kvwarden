#!/usr/bin/env bash
# publish_kvwarden_stub.sh — reserve PyPI `kvwarden` with a 0.0.1 placeholder.
#
# Run this before Show HN to pre-empt squatters. Does NOT touch src/kvwarden/
# in this repo. Works in a mktemp dir, cleans up on exit.
#
# Auth: set TWINE_USERNAME=__token__ and TWINE_PASSWORD=<pypi-api-token> in
# your shell, OR leave them unset and twine will prompt. Create a token at
# https://pypi.org/manage/account/token/ scoped to "Entire account" for the
# first upload, then narrow to "Project: kvwarden" afterward.

set -euo pipefail

STUB_DIR="$(mktemp -d -t kvwarden-stub-XXXXXX)"
trap 'rm -rf "$STUB_DIR"' EXIT

echo "[stub] Working in $STUB_DIR"
cd "$STUB_DIR"

cat > pyproject.toml <<'EOF'
[project]
name = "kvwarden"
version = "0.0.1"
description = "Tenant-fair LLM inference orchestration on a single GPU. Placeholder — see https://kvwarden.org."
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.11"
authors = [{name = "Shrey Patel", email = "patelshrey77@gmail.com"}]
keywords = ["llm", "inference", "vllm", "sglang", "multi-tenant", "fairness"]
classifiers = [
    "Development Status :: 1 - Planning",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Scientific/Engineering :: Artificial Intelligence",
]

[project.urls]
Homepage = "https://kvwarden.org"
Repository = "https://github.com/coconut-labs/kvwarden"

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"
EOF

cat > README.md <<'EOF'
# kvwarden

Placeholder for version 0.0.1. The real package ships with 0.1.0.

See https://kvwarden.org for the current release, benchmarks, and documentation.
EOF

mkdir -p src/kvwarden
cat > src/kvwarden/__init__.py <<'EOF'
"""kvwarden — placeholder package. See https://kvwarden.org."""

__version__ = "0.0.1"
EOF

echo "[stub] Installing build + twine"
python -m pip install --quiet --upgrade build twine

echo "[stub] Building sdist + wheel"
python -m build

echo "[stub] Uploading to PyPI"
if [ -n "${TWINE_USERNAME:-}" ] && [ -n "${TWINE_PASSWORD:-}" ]; then
    python -m twine upload dist/*
else
    # Interactive prompt — user enters __token__ and the token string.
    python -m twine upload dist/*
fi

echo "[stub] Done. Verify at https://pypi.org/project/kvwarden/"
