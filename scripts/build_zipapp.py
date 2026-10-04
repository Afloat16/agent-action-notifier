"""Build a dependency-free artifact without pip/build or network access."""

from pathlib import Path
import tempfile
import shutil
import zipapp

ROOT = Path(__file__).resolve().parents[1]
destination = ROOT / "dist" / "agent-action-notifier.pyz"
destination.parent.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="aan-build-") as directory:
    staging = Path(directory)
    shutil.copytree(ROOT / "src" / "agent_action_notifier", staging / "agent_action_notifier",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
    zipapp.create_archive(staging, destination, interpreter="/usr/bin/env python3",
                          main="agent_action_notifier.cli:main", compressed=True)
print(destination)
