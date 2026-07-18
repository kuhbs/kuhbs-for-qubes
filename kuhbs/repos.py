# Purpose: Local kuhb repository discovery helpers
# Scope: Repository reads stay side-effect free
from __future__ import annotations

from pathlib import Path

from .config import resolve_path


def local_kuhb_paths(kuhbs_root: str | Path) -> list[Path]:
    # Include every immediate KUHB directory so validation can report a missing kuhb.yml
    root = resolve_path(kuhbs_root)
    if not root.exists():
        # A fresh install can open the GUI before any kuhb definitions are downloaded
        return []
    # Dot directories are repository/editor metadata and cannot be valid KUHB ids
    return sorted(
        child / "kuhb.yml"
        for child in root.iterdir()
        if not child.name.startswith(".") and (child.is_dir() or child.is_symlink())
    )
