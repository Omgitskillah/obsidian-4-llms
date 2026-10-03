# Standalone tests

Obsidian's original package configuration expects a uv workspace containing
`access-logging`. The repository preserves that configuration and includes a
copy of the shared package in `vendor/access-logging`.

To run the same checks as GitHub Actions, choose a new temporary directory:

```bash
python3 ci/prepare-workspace.py /tmp/obsidian-ci
cd /tmp/obsidian-ci
uv sync --locked
VAULT_PATH=/tmp/ci-vault uv run --locked pytest servers/obsidian/tests tools/logging/tests
```

`ci/uv.lock` locks this standalone test workspace. To refresh it, run `uv lock`
in the staged workspace and copy its `uv.lock` back to `ci/uv.lock`.
The root package files, source, tests, and README are preserved from Nuru.
The original root `uv.lock` is retained as part of that snapshot; standalone
CI uses `ci/uv.lock`.
