# Purpose: Resolve KUHBS YAML into concrete VM models and lifecycle states
# Scope: Keep naming, defaults merging, and creation/removal order centralized
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable

from .state import STATE_ACTIONS

# Create templates before dependents; remove dependents before bases.
KUH_ORDER = ("tpl", "app", "ndp", "sta")
REMOVE_ORDER = ("ndp", "app", "tpl", "sta")
REQUIRED_KUHS_BY_TYPE = {
    "app": ("tpl", "app"),
    "ndp": ("tpl", "app", "ndp"),
    "udp": ("tpl", "app"),
    "sta": ("sta",),
}


@dataclass(frozen=True)
class Kuh:
    # Runtime view of one managed VM.  Unnamed disposables never appear here because they
    # are created by qvm-run --dispvm from launchers, not by KUHBS create/remove.
    kuhb_id: str
    kind: str
    name: str
    instance_id: str | None
    config: dict


def action_allowed(state: str, action: str) -> bool:
    # Multi-target strictness belongs to the shared Plan; this is one target's state matrix
    return action in STATE_ACTIONS[state]


def display_state(state: str) -> str:
    # Keep internal state slugs stable while presenting title-cased product labels.
    return {
        # Dom0 has no Create lifecycle, so its fresh gate is already ready for supported actions
        "dom0": "Ready",
        "linked": "Linked",
        "create:start": "Creating",
        "create:failed": "Create Failed",
        "create:completed": "Created",
        "backup:start": "Backing Up",
        "backup:failed": "Backup Failed",
        "backup:completed": "Backed Up",
        "restore:start": "Restoring",
        "restore:failed": "Restore Failed",
        "restore:completed": "Restored",
        "remove:start": "Removing",
        "remove:failed": "Remove Failed",
        "remove:completed": "Removed",
    }[state]



def _deep_merge(defaults: dict, override: dict) -> dict:
    # Dicts merge recursively so a kuhb can override one qvm-pref without repeating
    # every sibling pref.  Lists replace instead of append because setup-script and
    # launcher order is user intent and must not receive hidden extra items.
    merged = deepcopy(defaults)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _required_kinds(definition: dict) -> tuple[str, ...]:
    # The kuhb type declares the minimum topology; explicit YAML can add optional kinds.
    kuhb_type = definition.get("type")
    kinds = list(REQUIRED_KUHS_BY_TYPE.get(kuhb_type, ())) if isinstance(kuhb_type, str) else []
    for kind in definition.get("kuhs", {}).keys():
        if kind != "udp" and kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


def _default_instance_template(default_instances: list[dict]) -> dict:
    # defaults.yml keeps one id: default item as a per-instance template.  It is not
    # an extra VM and is not used to repair a missing id in a real kuhb.yml instance.
    for instance in default_instances:
        if instance.get("id") == "default":
            return instance
    raise ValueError("defaults.yml default_kuhb instance template missing id: default")


def _merge_instances(default_instances: list[dict], override_instances: list[dict], base_config: dict | None = None) -> list[dict]:
    # Apply the default instance template independently to every explicit instance
    template = _default_instance_template(default_instances)
    base = deepcopy(base_config or {})
    base.pop("instances", None)
    # setup_template configures the temporary standalone base and never belongs on final instances
    base.pop("setup_template", None)
    return [_deep_merge(_deep_merge(template, base), instance) for instance in override_instances]


def merge_launcher_config(defaults: dict, launcher: dict) -> dict:
    # Required launcher fields pass raw schema validation while optional fields receive real defaults
    return _deep_merge(defaults["default_launcher"], launcher)


def _merge_unit_launchers(defaults: dict, config: dict) -> dict:
    # Resolved KUH configs carry complete launcher values before operations receive them
    merged = deepcopy(config)
    merged["launchers"] = [merge_launcher_config(defaults, launcher) for launcher in merged.get("launchers", [])]
    return merged


def merge_kuhb_definition(defaults: dict, definition: dict) -> dict:
    # Normalize a user definition into the shape operations expect without editing the source YAML.
    # Start with scalar metadata defaults, then add only the kuh kinds that this kuhb
    # type actually manages.  default_kuhb contains ndp/sta examples, but an app kuhb
    # must not suddenly resolve those VMs just because defaults.yml documents them.
    base = deepcopy(defaults["default_kuhb"])
    default_kuhs = base.pop("kuhs")
    override = deepcopy(definition)
    raw_kuhs = override.pop("kuhs", {})
    merged = _deep_merge(base, override)
    merged["kuhs"] = {}
    for kind in _required_kinds(definition):
        if kind == "udp":
            continue
        kind_default = deepcopy(default_kuhs[kind])
        kind_override = deepcopy(raw_kuhs.get(kind, {}))
        kind_merged = _deep_merge(kind_default, kind_override)
        if "instances" in kind_merged:
            override_instances = kind_override.get("instances", [])
            kind_merged["instances"] = _merge_instances(kind_default["instances"], override_instances, kind_override)
            kind_merged["instances"] = [_merge_unit_launchers(defaults, instance) for instance in kind_merged["instances"]]
        else:
            kind_merged = _merge_unit_launchers(defaults, kind_merged)
        merged["kuhs"][kind] = kind_merged
    return merged


def resolve_kuh_name(kuhb_id: str, kind: str, instance_id: str | None = None) -> str:
    # Naming is centralized so create/remove/list/launcher code never drift apart.
    if instance_id and instance_id != "default":
        return f"{kind}-{kuhb_id}-{instance_id}"
    return f"{kind}-{kuhb_id}"


def resolve_kuhs(definition: dict) -> list[Kuh]:
    # Expand one validated, merged definition into concrete VMs in deterministic creation order
    kuhb_id = definition["id"]
    raw_kuhs = definition["kuhs"]
    resolved: list[Kuh] = []
    for kind in KUH_ORDER:
        if kind not in raw_kuhs:
            continue
        config = deepcopy(raw_kuhs[kind])
        if kind == "tpl":
            resolved.append(Kuh(kuhb_id, kind, resolve_kuh_name(kuhb_id, kind), None, config))
            continue
        instances = config["instances"]
        for instance_config in instances:
            instance_data = deepcopy(instance_config)
            instance_id = instance_data.pop("id")
            resolved.append(Kuh(kuhb_id, kind, resolve_kuh_name(kuhb_id, kind, instance_id), instance_id, instance_data))
    return resolved


def base_template_vm_names(definitions: Iterable[dict]) -> tuple[str, ...]:
    # Top-level creation bases are shared Qubes targets, so expose each active name once
    return tuple(sorted({definition["template"] for definition in definitions}))
