# Purpose: Setup-script fragment ordering and concatenation
# Scope: KUHBS owns the final shebang and visible xtrace run
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence


# All setup fragments become one visible Bash run with xtrace enabled
HEADER = "#!/bin/bash\nset -e -x\n"


def iter_script_files(paths: Sequence[str | Path]) -> list[Path]:
    # Explicit setup_scripts list entries run in YAML order.
    # Directory entries are expanded in stable filename order.
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"setup script path does not exist: {path}")
        if path.is_file():
            files.append(path)
        else:
            files.extend(sorted(child for child in path.iterdir() if child.is_file() and child.suffix == ".sh"))
    return files


def concatenate_script_fragments(paths: Sequence[str | Path]) -> str:
    # Combine trusted setup fragments in the exact order KUHBS will run them
    chunks = [HEADER]
    for script in iter_script_files(paths):
        text = script.read_text(encoding="utf-8")
        if text.startswith("#!"):
            # Fragments are not standalone scripts; KUHBS owns the shebang and strict mode
            raise ValueError(f"setup script fragment must not have shebang: {script}")
        chunks.append(f"\n# {script}\n")
        chunks.append(text)
        if not text.endswith("\n"):
            chunks.append("\n")
    return "".join(chunks)


def write_concatenated_script(path: str | Path, content: str) -> Path:
    # The output is executable because qvm-run invokes it directly inside the target kuh.
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    output.chmod(0o700)
    return output
