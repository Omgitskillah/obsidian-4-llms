# Standalone tests

The repository now installs directly, including `vendor/access-logging`:

```bash
uv sync --locked
VAULT_PATH=/tmp/test-vault uv run --locked pytest tests vendor/access-logging/tests
```

GitHub Actions uses this root lockfile and the same command. The older
`prepare-workspace.py` staging helper and `ci/uv.lock` are retained for the
original workspace layout; they are no longer the CI installation path.
