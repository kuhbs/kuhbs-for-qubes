# Purpose: Plain-file operation status for local kuhb action gating
# Scope: Track operation results while keeping upgrades outside lifecycle admission
from __future__ import annotations

from pathlib import Path


TRACKED_ACTIONS = {"create", "upgrade", "backup", "restore", "remove"}
# Upgrade records health only; create/backup/restore/remove continue to own lifecycle admission.
GATE_ACTIONS = ("create", "backup", "restore", "remove")
STATUS_WORDS = {"start", "completed", "failed"}

# This is the product matrix. State files describe the last operation result
# they do not claim that Qubes VMs or backup archives still exist
STATE_ACTIONS = {
    # Dom0 needs no Create, so a fresh state may start its first Backup or Upgrade directly
    "dom0": {"backup", "upgrade"},
    "linked": {"create", "unlink"},
    "create:failed": {"remove"},
    "create:completed": {"backup", "restore", "upgrade", "remove"},
    "backup:failed": {"backup", "restore", "upgrade", "remove"},
    "backup:completed": {"backup", "restore", "upgrade", "remove"},
    "restore:failed": {"restore", "remove"},
    "restore:completed": {"backup", "restore", "upgrade", "remove"},
    "remove:failed": {"remove"},
    "remove:completed": {"create", "unlink"},
    "create:start": set(),
    "backup:start": set(),
    "restore:start": set(),
    "remove:start": set(),
}


class StateStore:
    # Small text files keep the gate visible and easy to reset from dom0
    def __init__(self, root: str | Path):
        # Store constructor inputs on the object for the small helper methods below
        self.root = Path(root)

    def action_path(self, kuhb_id: str, action: str) -> Path:
        # Map a KUHB/action pair to its plain state file path
        return self.root / kuhb_id / action

    def get_state(self, kuhb_id: str, action: str) -> str | None:
        # Missing action files are absent; invalid contents raise
        path = self.action_path(kuhb_id, action)
        if not path.exists():
            return None
        state = path.read_text(encoding="utf-8").strip()
        if state not in STATUS_WORDS:
            raise ValueError(f"invalid state in {path}: {state}")
        return state

    def set_state(self, kuhb_id: str, action: str, state: str) -> None:
        # Write one operation gate value through the shared state store
        if action not in TRACKED_ACTIONS:
            raise ValueError(f"untracked action: {action}")
        if state not in STATUS_WORDS:
            raise ValueError(f"invalid state: {state}")
        path = self.action_path(kuhb_id, action)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{state}\n", encoding="utf-8")

    def current_gate(self, kuhb_id: str) -> str:
        # The newest status write describes the last operation attempted for this KUHB
        latest: tuple[int, str] | None = None
        for action in GATE_ACTIONS:
            path = self.action_path(kuhb_id, action)
            if not path.exists():
                continue
            modified_ns = path.stat().st_mtime_ns
            if latest is None or modified_ns > latest[0]:
                latest = (modified_ns, action)
        if latest is None:
            # Dom0 has no create/remove lifecycle; its first valid operations use the dom0 gate
            return "dom0" if kuhb_id == "dom0" else "linked"
        _modified_ns, action = latest
        state = self.get_state(kuhb_id, action)
        return f"{action}:{state}"

    def can_i(self, action: str, kuhb_id: str, *, linked: bool) -> tuple[bool, str]:
        # Apply the lifecycle action matrix to the current gate and link state
        if action == "link":
            return (not linked, f"cannot link {kuhb_id}; already linked" if linked else "")
        if action == "unlink":
            if not linked:
                return False, f"cannot unlink {kuhb_id}; not linked"
        elif not linked:
            return False, f"cannot {action} {kuhb_id}; not linked"

        gate = self.current_gate(kuhb_id)
        if gate.endswith(":start"):
            running = gate.split(":", 1)[0]
            return False, f"cannot {action} {kuhb_id}; {running} is running"
        allowed = action in STATE_ACTIONS[gate]
        return allowed, "" if allowed else f"cannot {action} {kuhb_id}; state is {gate}"
