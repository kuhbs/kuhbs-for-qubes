#!/usr/bin/env python3
# ONLY FOR DEVELOPMENT PURPOSES
# Mark linked KUHBs created when every resolved persistent VM already exists
# Do not use this to replace `kuhbs create` or `kuhbs backup`
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from kuhbs.config import load_defaults, load_kuhb_definition
from kuhbs.model import merge_kuhb_definition, resolve_kuhs
from kuhbs.state import StateStore

KUHBS_ROOT = Path.home() / ".kuhbs/my-kuhbs"
STATE_ROOT = Path.home() / ".kuhbs/states"
BACKUP_ROOT = "/mnt/kuhbs-backup"


def existing_vms() -> set[str]:
    result = subprocess.run(
        ["qvm-ls", "--raw-data", "--fields", "NAME"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        line.split("|", 1)[0].strip()
        for line in result.stdout.splitlines()
        if line.strip()
    }


def archive_exists(backup_vm: str, kuh_name: str) -> bool:
    result = subprocess.run(
        [
            "qvm-run",
            "--quiet",
            "--no-autostart",
            "--user",
            "root",
            backup_vm,
            f"test -f {BACKUP_ROOT}/{kuh_name}.tar.zst",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    defaults = load_defaults(REPO_ROOT / "defaults.yml")
    backup_vm = defaults["backup"]["kuh"]
    state_store = StateStore(STATE_ROOT)
    present = existing_vms()

    for definition_path in sorted(KUHBS_ROOT.glob("*/kuhb.yml")):
        raw_definition = load_kuhb_definition(definition_path)
        definition = merge_kuhb_definition(defaults, raw_definition)
        kuhb_id = definition["id"]
        kuhs = resolve_kuhs(definition)
        missing = [kuh.name for kuh in kuhs if kuh.name not in present]
        if missing:
            print(f"SKIP {kuhb_id} Missing: {', '.join(missing)}")
            continue

        state_store.set_state(kuhb_id, "create", "completed")
        print(f"STATUS {kuhb_id} Marked create state completed")

        backup_kuhs = [kuh for kuh in kuhs if "backup" in kuh.config]
        if backup_kuhs and all(
            archive_exists(backup_vm, kuh.name)
            for kuh in backup_kuhs
        ):
            state_store.set_state(kuhb_id, "backup", "completed")
            print(f"STATUS {kuhb_id} Marked backup state completed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
