# PyPI reservation — `kvwarden` stub

## When to run this

**Before Show HN.** The moment the new brand is public — even a tweet — someone will try to `pip install kvwarden` and find nothing, and a squatter can claim the name in the window between the rename becoming visible and the real 0.1.0 release. Ship the 0.0.1 placeholder first so the name is yours before any traffic arrives.

Quick checklist before you run the script:
- You're logged into PyPI as `patelshrey77@gmail.com`.
- You have a PyPI API token ready — create at https://pypi.org/manage/account/token/. Scope it to "Entire account" for this first upload; narrow to "Project: kvwarden" after the project page exists.
- You're OK with this being publicly visible the moment the upload finishes. PyPI does not offer private packages.

## One-liner

```bash
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-<your-token> \
  ./scripts/publish_kvwarden_stub.sh
```

Or run it without env vars and twine will prompt:

```bash
./scripts/publish_kvwarden_stub.sh
# username: __token__
# password: pypi-<your-token>
```

The script works in a temp directory and cleans itself up. It does not modify `src/kvwarden/` or anything else in this repo.

## After the upload

1. **Verify the project page renders.** Visit https://pypi.org/project/kvwarden/ and confirm: the description mentions https://kvwarden.org, the version is 0.0.1, the author email is right, the license is MIT. If any of that is wrong, fix the pyproject template in the script and re-upload as 0.0.2.
2. **Optionally add a longer README via the PyPI web UI.** Not required — the 0.0.1 README is already a one-liner pointing at kvwarden.org. Skip this unless you want the project page to look less barren before launch.
3. **Confirm `pip install kvwarden` works.** From a fresh venv, run `pip install kvwarden` and `python -c "import kvwarden; print(kvwarden.__version__)"`. Should print `0.0.1`. This is the squatter-proof.
4. **Tag the PyPI token as used once.** If you created an "Entire account" token for this upload, revoke it now and create a new one scoped to "Project: kvwarden" for future uploads.

## Bumping to 0.1.0 when the real release ships

When the real package is ready to ship from this repo:

1. Bump `pyproject.toml` in the main tree: `version = "0.1.0"`. (The 0.0.1 stub does not block a 0.1.0 upload — PyPI versions are ordered, not gated.)
2. Build and upload from the repo root:
   ```bash
   python -m build
   python -m twine upload dist/*
   ```
3. The project page at pypi.org/project/kvwarden/ will update to 0.1.0. The 0.0.1 wheel stays queryable via `pip install kvwarden==0.0.1` but `pip install kvwarden` will resolve to 0.1.0.
4. If you want to hide 0.0.1, mark it yanked in the PyPI web UI — it stays installable by version pin but disappears from the resolver for unpinned installs. Not required; most projects leave placeholder versions visible.

## Troubleshooting

- **`HTTPError: 403 Forbidden`** — the token is wrong or not scoped to upload. Regenerate a token with "Entire account" scope for the first upload.
- **`HTTPError: 400 File already exists`** — a version with this name + version number was already uploaded. Bump to 0.0.2 in the script and re-run.
- **`build` fails with `setuptools.build_meta` missing** — your Python is very old; upgrade to 3.11+.
- **The script runs but nothing appears on pypi.org** — check https://pypi.org/manage/projects/ logged in; sometimes propagation takes 60 seconds.
