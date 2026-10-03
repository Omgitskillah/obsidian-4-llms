"""Stage an isolated test workspace without editing the original packages."""
from pathlib import Path
import shutil
import sys

repo = Path(__file__).resolve().parents[1]
workspace = Path(sys.argv[1]).resolve()
if workspace == repo or repo in workspace.parents:
    raise SystemExit("Choose a workspace outside the repository")
workspace.mkdir(parents=True, exist_ok=False)
ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".logs", ".db", ".coverage", ".DS_Store", ".env", ".env.*", "vendor")
shutil.copytree(repo, workspace / "servers/obsidian", ignore=ignore)
shutil.copytree(repo / "vendor/access-logging", workspace / "tools/logging", ignore=ignore)
for name in ("pyproject.toml", "uv.lock"):
    source = repo / "ci" / name
    if source.exists():
        shutil.copy2(source, workspace / name)
