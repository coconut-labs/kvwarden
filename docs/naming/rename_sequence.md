# Rename sequence — the copy-paste playbook

This is the exact shell sequence that executes the tree sweep once preconditions hold. It is idempotent (re-running after partial completion is safe) and ends with a `git grep` audit that must return only expected hits. Run it from the repo root on a clean checkout of `origin/main`.

Reference: `rename_plan.md` for the reasoning, `user_checklist.md` for out-of-tree steps.

---

## 0. Preconditions — verify before starting

```bash
# All must be true before starting.
[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || { echo "Not on main"; exit 1; }
git pull --ff-only origin main
[[ -z "$(git status --porcelain)" ]] || { echo "Dirty tree"; exit 1; }

# Confirm the in-flight agents have landed.
# feat/bench-reproduce-hero — must be merged:
git log --oneline origin/main | head -20 | grep -iE 'reproduce.hero|bench.hero' \
  || { echo "reproduce-hero bench not yet merged"; exit 1; }

# RTX 4090 consumer run — presumed branch name results/consumer-4090-20260421.
# Verify it landed OR adjust branch name; if its artifacts live only under
# results/**, the sweep does not need to wait for it (results/** is excluded).
```

Out-of-tree blocking items from `user_checklist.md` — confirm manually:
- `pip index versions kvwarden` → returns 0.0.1 (you own the PyPI name)
- `npm view kvwarden` → shows your placeholder
- `dig +short kvwarden.org` → returns a Cloudflare IP (you own the domain)
- `gh repo view coconut-labs/kvwarden --json name` → name is `kvwarden`

If any of the above fails, stop and finish the checklist item first.

---

## 1. Create the sweep branch

```bash
git checkout -b rename/kvwarden-to-kvwarden
```

---

## 2. Directory rename — `src/kvwarden/` → `src/kvwarden/`

```bash
# Preserves git history through a rename-detection pass.
git mv src/kvwarden src/kvwarden

# Remove the generated egg-info — rebuild picks up the new name.
rm -rf src/kvwarden.egg-info src/kvwarden.egg-info
```

---

## 3. Define the exclusion pathspec once

Every subsequent `git grep` and sed pass honors this exclusion list. Save it to a variable so the incantations stay short.

```bash
EXCLUDE='
:(exclude)results/
:(exclude)docs/naming/kvwarden_name_audit.md
:(exclude)PROGRESS.md
:(exclude)*.tar.gz
:(exclude)*.egg-info/
:(exclude)__pycache__/
:(exclude).ruff_cache/
:(exclude).pytest_cache/
'

# `printf '%s\n' $EXCLUDE` expands correctly when quoted in `git grep`:
_grep() { git grep "$@" -- $EXCLUDE ; }
```

---

## 4. sed pass 1 — hyphen + underscore forms

Only two files in-tree reference these forms (the audit doc), but run the pass for completeness and future-proofing.

```bash
files="$(_grep -l -E 'kvwarden|kvwarden' | grep -v docs/naming/kvwarden_name_audit.md || true)"
if [[ -n "$files" ]]; then
  echo "$files" | xargs sed -i '' \
    -e 's/kvwarden/kvwarden/g' \
    -e 's/kvwarden/kvwarden/g'
fi
```

Note: on Linux use `sed -i` (no `''`). The `sed -i ''` form above is macOS BSD sed.

---

## 5. sed pass 2 — `KVWarden` → `KVWarden`

```bash
files="$(_grep -l 'KVWarden' || true)"
if [[ -n "$files" ]]; then
  echo "$files" | xargs sed -i '' 's/KVWarden/KVWarden/g'
fi
```

---

## 6. sed pass 3 — `KVWARDEN_` → `KVWARDEN_` (env vars, anchored)

The trailing underscore anchor protects against any hypothetical `KVWARDENX` collision (none exist today, but the anchor is free insurance).

```bash
files="$(_grep -l 'KVWARDEN_' || true)"
if [[ -n "$files" ]]; then
  echo "$files" | xargs sed -i '' 's/KVWARDEN_/KVWARDEN_/g'
fi
```

---

## 7. sed pass 4 — lowercase `kvwarden` → `kvwarden`

Runs last so it does not clobber any form handled above.

```bash
files="$(_grep -l 'kvwarden' || true)"
if [[ -n "$files" ]]; then
  echo "$files" | xargs sed -i '' 's/kvwarden/kvwarden/g'
fi
```

---

## 8. File renames — dashboards and grafana

```bash
git mv dashboards/kvwarden-fairness.json dashboards/kvwarden-fairness.json
git mv docs/grafana/kvwarden-overview.json docs/grafana/kvwarden-overview.json
```

(These also received content rewrites from the sed passes; the `git mv` here is just the filename.)

---

## 9. telemetry-worker wrangler.toml — **review and confirm**

```bash
# The sed passes already changed name = "kvwarden-telemetry" to "kvwarden-telemetry".
# But database_id was "REPLACE_WITH_WRANGLER_OUTPUT" on main and still needs
# the real id from the new D1 database created in user_checklist.md item 6.
# If user_checklist step 6 has been done, paste the new id here manually.
$EDITOR telemetry-worker/wrangler.toml
```

---

## 10. Quick sanity — package imports resolve

```bash
# Without installing — just the AST:
python -c "import ast; ast.parse(open('src/kvwarden/__init__.py').read())"

# Full install + import:
pip install -e ".[dev]"
python -c "import kvwarden; print(kvwarden.__version__)"

# CLI entry point:
kvwarden --help | head -5
```

---

## 11. Tests pass

```bash
pytest tests/unit/ -v --tb=short
```

If any test fails because it hard-codes a metric name with the old prefix that the sed didn't catch (e.g. inside a string literal split across lines), fix it and commit.

---

## 12. Lint and format

```bash
ruff check src/ tests/
ruff format --check src/ tests/

# If format check fails:
ruff format src/ tests/
```

---

## 13. The verification grep — must return nothing

```bash
_grep -iE 'KVWarden|kvwarden|KVWARDEN|kvwarden|kvwarden' && {
  echo "LEFTOVER HITS — inspect above, decide fix-or-exclude"
  exit 1
} || echo "VERIFICATION CLEAN"
```

Expected failure paths and fixes:
- Leftover in a docstring line that was too exotic for sed (e.g. quoted inside a raw-string) — hand-edit.
- Leftover in a committed log that slipped past the `results/` exclusion — add the specific file to `$EXCLUDE` in Section 3 of `rename_plan.md` and document why.

---

## 14. Commit in review-friendly chunks

```bash
# Chunk 1: directory rename (pure `git mv`, reviewers can skim).
git add -A src/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: src/kvwarden/ → src/kvwarden/ (directory move)"

# Chunk 2: pyproject.toml and build metadata.
git add pyproject.toml requirements-gpu.txt .gitignore
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: pyproject + build metadata (kvwarden → kvwarden)"

# Chunk 3: configs + benchmark scripts.
git add configs/ benchmarks/ scripts/ profiling/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: configs + scripts (kvwarden → kvwarden)"

# Chunk 4: docs + README.
git add README.md CONTRIBUTING.md SECURITY.md research_roadmap.md docs/ docker/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: docs + top-level (kvwarden → kvwarden)"

# Chunk 5: telemetry-worker + dashboards + grafana.
git add telemetry-worker/ dashboards/ docs/grafana/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: telemetry-worker + dashboards (kvwarden → kvwarden)"

# Chunk 6: tests.
git add tests/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: tests (kvwarden → kvwarden)"

# Chunk 7: .github/.
git add .github/
git commit --author="Shrey Patel <patelshrey77@gmail.com>" \
  -m "rename: .github issue templates + CI metadata"
```

If any of the above complains "nothing to commit" it means sed had no hits there — skip cleanly.

---

## 15. Push and open the sweep PR

```bash
git push -u origin rename/kvwarden-to-kvwarden

gh pr create \
  --title "rename: kvwarden → kvwarden (tree sweep)" \
  --body "$(cat <<'EOF'
Executes the rename playbook at docs/naming/rename_plan.md. All five spelling
forms swapped in the order documented there. results/**, PROGRESS.md, and
docs/naming/kvwarden_name_audit.md are deliberately untouched — they are
historical evidence.

Preconditions (all held before this PR): domains purchased, PyPI kvwarden
0.0.1 reserved, npm kvwarden + scope reserved, GitHub org repos renamed with
301s active. See docs/naming/user_checklist.md for the full checklist.

Post-merge follow-ups in docs/naming/user_checklist.md sections 6–8:
Cloudflare Worker cutover, PyPI kvwarden 0.1.3 deprecation stub, DNS
cutover for kvwarden.org.
EOF
)"
```

---

## 16. Post-merge — tag and publish

```bash
git checkout main && git pull
git tag v0.1.0
git push --tags

# Build and publish to PyPI (the kvwarden name is already reserved):
python -m build
python -m twine upload dist/*
```

Then run `user_checklist.md` sections 6, 7, 8 for the Worker cutover, the `kvwarden` 0.1.3 deprecation publish, and the DNS cutover.

---

## 17. Rollback — if the PR needs to be unwound

The sweep is a set of ordinary commits on a branch. To back out:

```bash
git checkout main
git branch -D rename/kvwarden-to-kvwarden
git push origin --delete rename/kvwarden-to-kvwarden
```

The PyPI `kvwarden` 0.0.1 placeholder remains (PyPI does not permit re-use of a version number, and the name reservation stands). The GitHub redirects remain. The domains remain. Those are all sunk-cost and useful even if the rename is aborted.
