# Purpose: Operation context and shared runtime dependencies
# Scope: Lazy properties create command and state helpers from defaults
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..command import CommandRunner
from ..config import resolve_path
from ..log import EventLogger
from ..model import resolve_kuhs
from ..state import StateStore


def definitions_by_id(definitions: tuple[dict, ...]) -> dict[str, dict]:
    # Startup validation already loaded definitions; index them without re-reading YAML
    return {definition["id"]: definition for definition in definitions}


@dataclass
class OperationContext:
    # Thin dependency bag shared by CLI, GUI worker processes, and tests with fake runners
    defaults: dict
    logger: EventLogger
    _runner: CommandRunner | None = field(default=None, init=False, repr=False)

    @property
    def config_root(self) -> Path:
        # Resolve the configured KUHBS state root
        return resolve_path(self.defaults["paths"]["config"])

    @property
    def kuhbs_root(self) -> Path:
        # Resolve the configured active KUHB root
        return resolve_path(self.defaults["paths"]["kuhbs"])

    @property
    def repos_root(self) -> Path:
        # Keep repository checkouts under the KUHBS state root
        return self.config_root / "repos"

    @property
    def state_store(self) -> StateStore:
        # Use plain lifecycle files under the KUHBS state root
        return StateStore(self.config_root / "states")

    def set_state(self, kuhb_id: str, action: str, state: str) -> None:
        # Write one operation gate value through the shared state store
        self.state_store.set_state(kuhb_id, action, state)
        self.logger.status(kuhb_id, f"State changed: {action}:{state}")

    @property
    def runner(self) -> CommandRunner:
        # One CLI command needs one process tracker so Ctrl-C can kill every child it started
        if self._runner is None:
            self._runner = CommandRunner(self.logger)
        return self._runner

    def register_kuh_labels(self, definition: dict) -> None:
        # Register source colors once from resolved config; command execution still happens in dom0.
        if "logging" in self.defaults:
            self.logger.configure_colors(self.defaults["logging"]["colors"])
        # A kuhb source is not a Qubes label; it gets the fixed KUHBS purple brand color.
        self.logger.set_source_label(definition["id"], "purple")
        for kuh in resolve_kuhs(definition):
            merged = kuh.config
            if "label" in merged["prefs"]:
                self.logger.set_source_label(kuh.name, str(merged["prefs"]["label"]))

__all__ = ["OperationContext", "definitions_by_id"]
