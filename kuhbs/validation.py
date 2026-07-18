# Purpose: Validate defaults.yml and every active or candidate kuhb.yml before mutation
# Scope: Standard schemas own structure while Python owns cross-field and filesystem semantics
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree
from typing import Any, Iterable
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import UnknownDialect

from .config import load_defaults, load_kuhb_definition, load_yaml, repo_defaults_path, resolve_path
from .model import REQUIRED_KUHS_BY_TYPE, Kuh, base_template_vm_names, merge_kuhb_definition, resolve_kuhs
from .qubes_status import QUBES_XML, VmInfo, read_qubes_xml
from .repos import local_kuhb_paths
from .state import STATUS_WORDS, TRACKED_ACTIONS



SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
QUBES_VM_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,30}")
CRYPTSETUP_MAPPER_NAME_RE = re.compile(rb"(?!\.+\Z)[^/\x00-\x1f\x7f]{1,127}")
RESERVED_KUHB_ID_RE = re.compile(r"disp[0-9]*")


@dataclass(frozen=True)
class ConfigIssue:
    # One validation message names the file the user must repair
    path: Path
    message: str


@dataclass(frozen=True)
class BrokenKuhb:
    # The active directory name is authoritative even when YAML has no usable id
    active_id: str
    path: Path
    issues: tuple[ConfigIssue, ...]
    definition: dict | None = None

    @property
    def error(self) -> ConfigValidationError:
        return ConfigValidationError(self.issues)


@dataclass(frozen=True)
class ValidatedConfig:
    # Commands receive one immutable snapshot: valid definitions stay usable and broken links stay visible
    defaults_path: Path
    defaults: dict
    kuhb_definitions: tuple[dict, ...]
    broken_kuhbs: tuple[BrokenKuhb, ...] = ()
    qubes_vms: dict[str, VmInfo] = field(default_factory=dict)


class ConfigValidationError(ValueError):
    # Collect every known configuration problem into one CLI and GUI friendly exception
    def __init__(self, issues: Iterable[ConfigIssue], *, definition: dict | None = None):
        self.issues = tuple(issues)
        self.definition = definition
        super().__init__(format_issues(self.issues))


def format_issues(issues: Iterable[ConfigIssue]) -> str:
    # Keep multi-file failures readable without exposing Python tracebacks
    lines = ["Configuration invalid"]
    for issue in issues:
        lines.append(f"{issue.path}: {issue.message}")
    return "\n".join(lines)


def _path_label(path: Iterable[Any]) -> str:
    # Convert schema paths into labels such as kuhs.app.instances[0].prefs.label
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text


def _schema_entry(schema: dict, name: str) -> dict:
    # Raw and resolved validation share one schema definition bank without duplicating rules
    return {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": f"#/$defs/{name}",
    }


def _load_schema_validators() -> tuple[Draft202012Validator, Draft202012Validator, Draft202012Validator]:
    # Product schemas ship beside the installed Python package under /usr/share/kuhbs
    defaults_path = SCHEMA_ROOT / "defaults.schema.yml"
    kuhb_path = SCHEMA_ROOT / "kuhb.schema.yml"
    try:
        defaults_schema = load_yaml(defaults_path)
        kuhb_schema = load_yaml(kuhb_path)
        raw_schema = _schema_entry(kuhb_schema, "raw")
        resolved_schema = _schema_entry(kuhb_schema, "resolved")
        Draft202012Validator.check_schema(kuhb_schema)
        Draft202012Validator.check_schema(defaults_schema)
        # Resource detection rejects an installed schema that declares an unsupported dialect
        Resource.from_contents(defaults_schema)
        registry = Registry().with_resource(
            kuhb_schema["$id"],
            Resource.from_contents(kuhb_schema),
        )
    except (OSError, KeyError, TypeError, ValueError, SchemaError, UnknownDialect) as exc:
        raise ConfigValidationError([ConfigIssue(SCHEMA_ROOT, f"invalid installed schema: {exc}")]) from exc
    return (
        Draft202012Validator(defaults_schema, registry=registry),
        Draft202012Validator(raw_schema),
        Draft202012Validator(resolved_schema),
    )


def _schema_error_messages(error) -> list[str]:
    # Translate standard JSON Schema failures into the concise wording used by KUHBS
    label = _path_label(error.absolute_path)
    prefix = f"{label} " if label else ""
    if error.validator == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        messages = []
        for key in missing:
            missing_label = f"{label + '.' if label else ''}{key}"
            if key == "instances":
                messages.append(f"{missing_label} must have at least one item")
            else:
                messages.append(f"{missing_label} is required")
        return messages
    if error.validator == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        unknown = [key for key in error.instance if key not in allowed]
        return [f"{label + '.' if label else ''}{key} is not a supported key" for key in unknown]
    if error.validator == "type":
        expected = error.validator_value
        if expected == "boolean":
            return [f"{prefix}must be True or False"]
        if expected == "object":
            return [f"{prefix}must be a mapping"]
        if expected == "array":
            if label == "backup.dom0_paths":
                return ["backup.dom0_paths must be a list of non-empty strings"]
            return [f"{prefix}must be a list"]
        if expected == "integer":
            if label.startswith("batch_size.") or label in {"logging.spacing", "repos.commit_selection_count", "i3_integration.notify_send_timeout_ms"}:
                return [f"{prefix}must be a positive integer"]
            return [f"{prefix}must be an integer"]
        if expected == "number":
            if label in {"backup.max_age_hours", "upgrade.max_age_minutes"}:
                return [f"{prefix}must be a positive number"]
            return [f"{prefix}must be a number"]
        if expected == "string":
            return [f"{prefix}must be a string"]
        return [f"{prefix}has the wrong type"]
    if error.validator == "enum":
        return [f"{prefix}must be one of {', '.join(str(value) for value in error.validator_value)}"]
    if error.validator == "const":
        return [f"{prefix}must be {error.validator_value}"]
    if error.validator == "minItems":
        return [f"{prefix}must have at least one item"]
    if error.validator in {"minimum", "exclusiveMinimum"}:
        if isinstance(error.instance, int) and not isinstance(error.instance, bool):
            return [f"{prefix}must be a positive integer"]
        return [f"{prefix}must be a positive number"]
    if error.validator == "maximum":
        return [f"{prefix}must be at most {error.validator_value}"]
    if error.validator == "minLength":
        return [f"{prefix}must be a non-empty string"]
    if error.validator == "maxLength":
        return [f"{prefix}is too long"]
    if error.validator == "pattern":
        if label == "icon" and isinstance(error.instance, str) and _has_control_chars(error.instance):
            return ["icon must not contain control characters"]
        return [f"{prefix}has invalid format"]
    if error.validator in {"oneOf", "anyOf"}:
        if label.endswith("netvm") and str(error.instance).lower() == "none":
            return [f"{prefix}must use YAML null instead of textual None"]
    return [f"{prefix}{error.message}"]


def _schema_issues(path: Path, validator: Draft202012Validator, data: dict) -> list[ConfigIssue]:
    # Sort errors by YAML path so repeated checks produce stable output and tests
    issues: list[ConfigIssue] = []
    try:
        errors = sorted(validator.iter_errors(data), key=lambda error: tuple(str(part) for part in error.absolute_path))
    except Unresolvable as exc:
        raise ConfigValidationError([ConfigIssue(SCHEMA_ROOT, f"invalid installed schema reference: {exc}")]) from exc
    except (RecursionError, TypeError, ValueError) as exc:
        raise ConfigValidationError([ConfigIssue(path, f"schema validation could not read YAML value: {exc}")]) from exc
    for error in errors:
        issues.extend(ConfigIssue(path, message) for message in _schema_error_messages(error))
    return issues


def _has_control_chars(value: str) -> bool:
    # YAML strings become paths, command arguments, logs, or desktop-entry fields and stay single-line
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_dom0_config_path(path: Path, label: str, value: str) -> list[ConfigIssue]:
    # User-facing dom0 paths are absolute or explicitly relative to the user's home
    if value == "~":
        return []
    if value.startswith("~/"):
        tail = value[2:]
    elif value.startswith("/"):
        tail = value[1:]
    else:
        return [ConfigIssue(path, f"{label} must be an absolute path or start with ~/")]
    if any(part in {"", ".", ".."} for part in tail.split("/")):
        return [ConfigIssue(path, f"{label} must not contain ., .., or // path components")]
    return []


def _validate_backup_path(path: Path, label: str, value: str) -> list[ConfigIssue]:
    # Backup restore matching supports absolute paths plus simple * shell globs only
    if not value.startswith("/"):
        return [ConfigIssue(path, f"{label} must be an absolute path")]
    if value == "/":
        return [ConfigIssue(path, f"{label} must not be / because it would include system paths like /dev")]
    if re.fullmatch(r"/(?:[A-Za-z0-9_./*-]|\\ )+", value) is None:
        return [ConfigIssue(path, f"{label} supports only absolute paths and * globs")]
    # Split the original text because pathlib normalizes explicit dot components away
    parts = value.split("/")
    if ".." in parts or "." in parts or "//" in value:
        return [ConfigIssue(path, f"{label} must not contain ., .., or // path components")]
    return []


def _validate_qubes_vm_reference(path: Path, label: str, value: str | None) -> list[ConfigIssue]:
    # Direct Qubes references use the same static-name contract as resolved KUH VM names
    if value is not None and QUBES_VM_NAME_RE.fullmatch(value) is None:
        return [
            ConfigIssue(
                path,
                f"{label} must begin with an ASCII letter and use only ASCII letters, digits, _, or -",
            )
        ]
    return []


def _validate_defaults_semantics(path: Path, defaults: dict) -> list[ConfigIssue]:
    # Schema owns shapes while this function checks path and merge contracts
    issues: list[ConfigIssue] = []
    for key, value in defaults["paths"].items():
        issues.extend(_validate_dom0_config_path(path, f"paths.{key}", value))
    for index, value in enumerate(defaults["backup"]["dom0_paths"]):
        issues.extend(_validate_backup_path(path, f"backup.dom0_paths[{index}]", value))
    crypt_name = defaults["backup"]["crypt_name"]
    if CRYPTSETUP_MAPPER_NAME_RE.fullmatch(crypt_name.encode("utf-8")) is None:
        issues.append(
            ConfigIssue(
                path,
                "backup.crypt_name must be a valid cryptsetup mapper name: 1-127 UTF-8 bytes, no / or control characters, and not only dots",
            )
        )
    qubes_references = [
        ("repos.dispvm_template", defaults["repos"]["dispvm_template"]),
        ("backup.kuh", defaults["backup"]["kuh"]),
    ]
    snitch = defaults["firewall"]["qubes_snitch"]
    if snitch is not None:
        qubes_references.append(("firewall.qubes_snitch.snitch_vm", snitch["snitch_vm"]))
    qubes_references.extend(
        (f"upgrade.restart_without_prompt[{index}]", value)
        for index, value in enumerate(defaults["upgrade"]["restart_without_prompt"])
    )
    default_kuhs = defaults["default_kuhb"]["kuhs"]
    qubes_references.append(("default_kuhb.kuhs.tpl.prefs.netvm", default_kuhs["tpl"]["prefs"]["netvm"]))
    qubes_references.append(
        (
            "default_kuhb.kuhs.sta.setup_template.prefs.netvm",
            default_kuhs["sta"]["setup_template"]["prefs"]["netvm"],
        )
    )
    for kind in ("app", "ndp", "sta"):
        for index, instance in enumerate(default_kuhs[kind]["instances"]):
            qubes_references.append(
                (f"default_kuhb.kuhs.{kind}.instances[{index}].prefs.netvm", instance["prefs"]["netvm"])
            )
    for label, value in qubes_references:
        issues.extend(_validate_qubes_vm_reference(path, label, value))
    for kind in ("app", "ndp", "sta"):
        instances = defaults["default_kuhb"]["kuhs"][kind]["instances"]
        if not any(instance["id"] == "default" for instance in instances):
            issues.append(ConfigIssue(path, f"default_kuhb.kuhs.{kind}.instances must contain id: default"))
    return issues


def _validate_topology(path: Path, raw: dict, merged: dict) -> list[ConfigIssue]:
    # KUHB type selects the minimum topology while explicit optional kinds still own dependencies
    issues: list[ConfigIssue] = []
    raw_kuhs = raw["kuhs"]
    kuhb_type = merged["type"]
    for kind in REQUIRED_KUHS_BY_TYPE[kuhb_type]:
        if kind != "tpl" and kind not in raw_kuhs:
            issues.append(ConfigIssue(path, f"kuhb type {kuhb_type} requires kuhs: {kind}"))
    if "app" in raw_kuhs and kuhb_type == "sta" and "tpl" not in raw_kuhs:
        issues.append(ConfigIssue(path, "configured kuhs.app requires kuhs.tpl"))
    if "ndp" in raw_kuhs and "app" not in raw_kuhs:
        issues.append(ConfigIssue(path, "configured kuhs.ndp requires kuhs.app"))
        return issues
    if "ndp" in raw_kuhs:
        default_apps = [instance for instance in merged["kuhs"]["app"]["instances"] if instance["id"] == "default"]
        if not default_apps:
            issues.append(ConfigIssue(path, "configured kuhs.ndp requires kuhs.app.instances with id: default"))
        elif default_apps[0]["prefs"]["template_for_dispvms"] is not True:
            issues.append(ConfigIssue(path, "configured kuhs.ndp requires default app prefs.template_for_dispvms: True"))
    if kuhb_type in {"ndp", "udp"} and "app" in merged["kuhs"]:
        default_seen = False
        for index, instance in enumerate(merged["kuhs"]["app"]["instances"]):
            default_seen = default_seen or instance["id"] == "default"
            if instance["prefs"]["template_for_dispvms"] is not True:
                issues.append(ConfigIssue(path, f"kuhb type {kuhb_type} requires kuhs.app.instances[{index}].prefs.template_for_dispvms: True"))
        if kuhb_type == "ndp" and not default_seen:
            issues.append(ConfigIssue(path, "kuhb type ndp requires kuhs.app.instances with id: default"))
    return issues


def _vm_name_roles(definition: dict, kuhs: list[Kuh]) -> list[tuple[str, str]]:
    # Include temporary setup VMs because they must not collide with another definition's persistent VM
    names = [(kuh.name, "resolved KUH") for kuh in kuhs]
    if "sta" in definition["kuhs"]:
        names.append((f"tpl-{definition['id']}-setup-tmp", "standalone setup template"))
    return names


def _validate_kuhb_references(path: Path, definition: dict, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # Validate configured VM references separately from the generated names of the KUHs themselves
    issues = _validate_qubes_vm_reference(path, "template", definition["template"])
    if definition["template"] == "dom0":
        # dom0 has its own upgrade phase and can never be routed through TemplateVM operations
        issues.append(ConfigIssue(path, "template must name a TemplateVM, not dom0"))
    for kuh in kuhs:
        issues.extend(
            _validate_qubes_vm_reference(
                path,
                f"{kuh.name}.prefs.netvm",
                kuh.config["prefs"]["netvm"],
            )
        )
    if "sta" in definition["kuhs"]:
        issues.extend(
            _validate_qubes_vm_reference(
                path,
                "kuhs.sta.setup_template.prefs.netvm",
                definition["kuhs"]["sta"]["setup_template"]["prefs"]["netvm"],
            )
        )
    return issues


def _validate_names(path: Path, definition: dict, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # Validate every persistent VM plus the temporary standalone setup VM derived outside resolve_kuhs
    issues: list[ConfigIssue] = []
    kuhb_id = definition["id"]
    seen_names: dict[str, str] = {}
    if kuhb_id == "dom0" or RESERVED_KUHB_ID_RE.fullmatch(kuhb_id):
        issues.append(ConfigIssue(path, "id is reserved; use a non-dom0, non-disp KUHB id"))
    for vm_name, role in _vm_name_roles(definition, kuhs):
        if QUBES_VM_NAME_RE.fullmatch(vm_name) is None:
            if role == "standalone setup template":
                issues.append(ConfigIssue(path, f"standalone setup template resolves to invalid Qubes VM name: {vm_name}"))
            else:
                issues.append(ConfigIssue(path, f"{vm_name} resolves to invalid Qubes VM name"))
        if vm_name in seen_names:
            if role == "resolved KUH" and seen_names[vm_name] == "resolved KUH":
                issues.append(ConfigIssue(path, f"duplicate resolved KUH name {vm_name}"))
            else:
                issues.append(ConfigIssue(path, f"duplicate Qubes VM name {vm_name}"))
        seen_names[vm_name] = role
    return issues


def _validate_svg(definition_path: Path, svg: Path, label: str) -> list[ConfigIssue]:
    try:
        root = ElementTree.parse(svg).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        return [ConfigIssue(definition_path, f"{label} is not a valid SVG: {exc}")]
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        return [ConfigIssue(definition_path, f"{label} is not a valid SVG: root element must be svg")]
    return []


def _validate_icons(path: Path, definition: dict, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # The KUHB card icon and every launcher icon are required SVG assets owned by the definition
    icon_value = definition["icon"]
    icon = Path(icon_value)
    if not icon.is_absolute():
        icon = path.parent / icon
    if not icon.is_file():
        return [ConfigIssue(path, f"icon file does not exist: {icon_value}")]
    issues = _validate_svg(path, icon, "icon file")

    launcher_dir = path.parent / "launcher-icons"
    required: set[Path] = set()
    for kuh in kuhs:
        for launcher in kuh.config["launchers"]:
            launcher_icon = launcher_dir / f"{launcher['id']}.svg"
            required.add(launcher_icon)
            if not launcher_icon.is_file():
                issues.append(ConfigIssue(path, f"launcher icon file does not exist: launcher-icons/{launcher['id']}.svg"))
    for launcher_icon in sorted(set(launcher_dir.glob("*svg")) | required):
        if launcher_icon.is_file():
            issues.extend(_validate_svg(path, launcher_icon, f"launcher icon {launcher_icon.name}"))
    return issues


def _validate_setup(path: Path, definition: dict, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # Validate only YAML setup placement; runtime owns the external script files themselves
    issues: list[ConfigIssue] = []
    for kuh in kuhs:
        if kuh.kind == "ndp" and kuh.config["setup_scripts"]:
            issues.append(ConfigIssue(path, f"{kuh.name}.setup_scripts must not be defined; use ndp hooks"))
        for index, value in enumerate(kuh.config["setup_scripts"]):
            if not resolve_path(value).is_absolute():
                issues.append(ConfigIssue(path, f"{kuh.name}.setup_scripts[{index}] must be an absolute path"))
    if "sta" in definition["kuhs"]:
        setup = definition["kuhs"]["sta"]["setup_template"]
        for index, value in enumerate(setup["setup_scripts"]):
            if not resolve_path(value).is_absolute():
                issues.append(ConfigIssue(path, f"standalone setup template setup_scripts[{index}] must be an absolute path"))
    return issues


def _validate_backups(path: Path, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # Backup configuration applies only to persistent tpl, app, and sta resources
    issues: list[ConfigIssue] = []
    for kuh in kuhs:
        backup = kuh.config.get("backup")
        if backup is None:
            continue
        if kuh.kind not in {"tpl", "app", "sta"}:
            issues.append(ConfigIssue(path, f"{kuh.name}.backup is not supported for {kuh.kind}"))
            continue
        for index, value in enumerate(backup["paths"]):
            issues.extend(_validate_backup_path(path, f"{kuh.name}.backup.paths[{index}]", value))
    return issues


def _validate_launchers(path: Path, definition: dict, kuhs: list[Kuh]) -> list[ConfigIssue]:
    # Launcher relationships depend on the resolved target VM and KUHB type rather than YAML shape alone
    issues: list[ConfigIssue] = []
    seen: set[tuple[str, str, str]] = set()
    sources = {kuh.name for kuh in kuhs if kuh.kind == "app" and kuh.config["prefs"].get("template_for_dispvms") is True}
    for kuh in kuhs:
        for index, launcher in enumerate(kuh.config["launchers"]):
            label = f"{kuh.name}.launchers[{index}]"
            key = (kuh.name, launcher["user"], launcher["id"])
            if key in seen:
                issues.append(ConfigIssue(path, f"{label} duplicates launcher id/user for this target"))
            seen.add(key)
            if launcher["dispvm"] is True and kuh.name not in sources:
                issues.append(ConfigIssue(path, f"{label}.dispvm requires an app with template_for_dispvms: True"))
    return issues


def _validate_kuhb_semantics(path: Path, defaults: dict, merged: dict) -> list[ConfigIssue]:
    # Structural schemas guarantee safe indexing before these product relationships run
    kuhs = resolve_kuhs(merged)
    issues: list[ConfigIssue] = []
    issues.extend(_validate_kuhb_references(path, merged, kuhs))
    issues.extend(_validate_names(path, merged, kuhs))
    issues.extend(_validate_icons(path, merged, kuhs))
    issues.extend(_validate_backups(path, kuhs))
    issues.extend(_validate_setup(path, merged, kuhs))
    issues.extend(_validate_launchers(path, merged, kuhs))
    return issues


def validate_kuhb_file(
    defaults: dict,
    path: str | Path,
    *,
    raw_validator: Draft202012Validator | None = None,
    resolved_validator: Draft202012Validator | None = None,
    qubes_vms: dict[str, VmInfo] | None = None,
) -> dict:
    # Candidate link and startup validation use this exact raw, merge, resolved, semantic pipeline
    definition_path = Path(path)
    if definition_path.is_symlink() or not definition_path.is_file():
        raise ConfigValidationError([ConfigIssue(definition_path, "kuhb.yml must be a real file")])
    if raw_validator is None or resolved_validator is None:
        _defaults_validator, raw_validator, resolved_validator = _load_schema_validators()
    try:
        raw = load_kuhb_definition(definition_path)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigValidationError([ConfigIssue(definition_path, str(exc))]) from exc
    issues = _schema_issues(definition_path, raw_validator, raw)
    if issues:
        raise ConfigValidationError(issues, definition=raw)
    merged = merge_kuhb_definition(defaults, raw)
    issues = _validate_topology(definition_path, raw, merged)
    if issues:
        raise ConfigValidationError(issues, definition=raw)
    issues = _schema_issues(definition_path, resolved_validator, merged)
    if issues:
        raise ConfigValidationError(issues, definition=raw)
    issues.extend(_validate_kuhb_semantics(definition_path, defaults, merged))
    if qubes_vms is not None:
        template = qubes_vms.get(merged["template"])
        if template is None:
            issues.append(ConfigIssue(definition_path, f"template VM does not exist: {merged['template']}"))
        elif template.klass != "TemplateVM":
            issues.append(ConfigIssue(definition_path, f"template VM must be a TemplateVM: {template.name} is {template.klass}"))
    if issues:
        raise ConfigValidationError(issues, definition=raw)
    return merged


def _partition_definition_set(
    entries: Iterable[tuple[str, Path, dict]],
) -> tuple[tuple[dict, ...], dict[str, tuple[ConfigIssue, ...]]]:
    # Cross-definition failures belong to every involved active link, never only the later one
    source = list(entries)
    issues_by_active: dict[str, list[ConfigIssue]] = {active_id: [] for active_id, _path, _definition in source}
    id_groups: dict[str, list[tuple[str, Path]]] = {}
    vm_groups: dict[str, list[tuple[str, Path, str]]] = {}
    template_names = set(base_template_vm_names(definition for _active_id, _path, definition in source))

    for active_id, path, definition in source:
        kuhb_id = definition["id"]
        if kuhb_id in template_names:
            issues_by_active[active_id].append(
                ConfigIssue(path, f"KUHB id {kuhb_id} is reserved by configured base TemplateVM {kuhb_id}")
            )
        if kuhb_id != active_id:
            issues_by_active[active_id].append(
                ConfigIssue(path, f"id must match KUHB directory/link name {active_id}: got {kuhb_id}")
            )
        id_groups.setdefault(kuhb_id, []).append((active_id, path))
        for vm_name, role in _vm_name_roles(definition, resolve_kuhs(definition)):
            vm_groups.setdefault(vm_name, []).append((active_id, path, role))

    for kuhb_id, group in id_groups.items():
        if len(group) < 2:
            continue
        for active_id, path in group:
            others = ", ".join(str(other_path) for other_id, other_path in group if other_id != active_id)
            issues_by_active[active_id].append(ConfigIssue(path, f"duplicate KUHB id {kuhb_id}; also defined in {others}"))

    for vm_name, group in vm_groups.items():
        active_ids = {active_id for active_id, _path, _role in group}
        if len(active_ids) < 2:
            continue
        all_resolved = all(role == "resolved KUH" for _active_id, _path, role in group)
        collision = "duplicate resolved KUH name" if all_resolved else "duplicate Qubes VM name"
        for active_id, path, _role in group:
            others = ", ".join(
                str(other_path)
                for other_id, other_path, _other_role in group
                if other_id != active_id
            )
            issues_by_active[active_id].append(ConfigIssue(path, f"{collision} {vm_name}; also defined in {others}"))

    broken = {
        active_id: tuple(active_issues)
        for active_id, active_issues in issues_by_active.items()
        if active_issues
    }
    valid = tuple(
        definition
        for active_id, _path, definition in source
        if active_id not in broken
    )
    return valid, broken


def validate_definition_set(defaults: dict, entries: Iterable[tuple[str, Path, dict]]) -> tuple[dict, ...]:
    # Mutation preflight remains strict while startup uses the same partitioned collision ownership
    del defaults
    definitions, broken = _partition_definition_set(entries)
    if broken:
        raise ConfigValidationError(issue for issues in broken.values() for issue in issues)
    return definitions


def validate_definition_changes(
    defaults: dict,
    entries: Iterable[tuple[str, Path, dict]],
    changed_active_ids: set[str],
) -> None:
    # Prospective mutations may ignore unrelated existing failures but every changed definition must be valid
    del defaults
    _definitions, broken = _partition_definition_set(entries)
    issues = [
        issue
        for active_id in sorted(changed_active_ids)
        for issue in broken.get(active_id, ())
    ]
    if issues:
        raise ConfigValidationError(issues)


def _lifecycle_issues(state_root: Path, active_id: str) -> list[ConfigIssue]:
    # Lifecycle corruption is local to the active card whose state directory owns the file
    issues: list[ConfigIssue] = []
    for action in sorted(TRACKED_ACTIONS):
        path = state_root / active_id / action
        if not path.exists():
            continue
        try:
            state = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            issues.append(ConfigIssue(path, f"cannot read lifecycle state: {exc}"))
            continue
        if state not in STATUS_WORDS:
            issues.append(
                ConfigIssue(
                    path,
                    f'invalid lifecycle state "{state}"; expected start, completed, or failed',
                )
            )
    return issues

def inspect_startup_config(defaults_path: str | Path | None = None, *, check_qubes: bool = False) -> ValidatedConfig:
    # Inspect active definitions independently while defaults and installed schemas remain hard gates
    resolved_defaults_path = Path(defaults_path) if defaults_path is not None else repo_defaults_path()
    try:
        defaults = load_defaults(resolved_defaults_path)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigValidationError([ConfigIssue(resolved_defaults_path, str(exc))]) from exc
    defaults_validator, raw_validator, resolved_validator = _load_schema_validators()
    issues = _schema_issues(resolved_defaults_path, defaults_validator, defaults)
    if not issues:
        issues.extend(_validate_defaults_semantics(resolved_defaults_path, defaults))
    if issues:
        raise ConfigValidationError(issues)

    qubes_vms = None
    if check_qubes:
        xml_path = QUBES_XML
        try:
            if not xml_path.is_file():
                raise FileNotFoundError(xml_path)
            qubes_vms = read_qubes_xml(xml_path)
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            raise ConfigValidationError([ConfigIssue(xml_path, f"cannot read Qubes configuration: {exc}")]) from exc

    entries: list[tuple[str, Path, dict]] = []
    broken_by_active: dict[str, BrokenKuhb] = {}
    kuhbs_root = resolve_path(defaults["paths"]["kuhbs"])
    repos_root = resolve_path(defaults["paths"]["config"]) / "repos"
    try:
        kuhb_paths = local_kuhb_paths(kuhbs_root)
    except OSError as exc:
        raise ConfigValidationError([ConfigIssue(kuhbs_root, f"cannot read active KUHB directory: {exc}")]) from exc
    for kuhb_path in kuhb_paths:
        kuhb_dir = kuhb_path.parent
        active_id = kuhb_dir.name
        try:
            if kuhb_dir.is_symlink() and not kuhb_dir.resolve().is_relative_to(repos_root.resolve()):
                issue = ConfigIssue(kuhb_path, f"linked KUHB must point inside {repos_root}")
                broken_by_active[active_id] = BrokenKuhb(active_id, kuhb_path, (issue,))
                continue
            definition = validate_kuhb_file(
                defaults,
                kuhb_path,
                raw_validator=raw_validator,
                resolved_validator=resolved_validator,
                qubes_vms=qubes_vms,
            )
            entries.append((active_id, kuhb_path, definition))
        except ConfigValidationError as exc:
            broken_by_active[active_id] = BrokenKuhb(active_id, kuhb_path, exc.issues, exc.definition)
        except (OSError, RuntimeError, ValueError) as exc:
            issue = ConfigIssue(kuhb_path, str(exc))
            broken_by_active[active_id] = BrokenKuhb(active_id, kuhb_path, (issue,))

    definitions, set_issues = _partition_definition_set(entries)
    entry_by_active = {
        active_id: (path, definition)
        for active_id, path, definition in entries
    }
    for active_id, active_issues in set_issues.items():
        path, definition = entry_by_active[active_id]
        broken_by_active[active_id] = BrokenKuhb(active_id, path, active_issues, definition)

    state_root = resolve_path(defaults["paths"]["config"]) / "states"
    valid_definitions: list[dict] = []
    for definition in definitions:
        active_id = definition["id"]
        path, raw_definition = entry_by_active[active_id]
        state_issues = _lifecycle_issues(state_root, active_id)
        if state_issues:
            broken_by_active[active_id] = BrokenKuhb(active_id, path, tuple(state_issues), raw_definition)
        else:
            valid_definitions.append(definition)

    broken = tuple(broken_by_active[active_id] for active_id in sorted(broken_by_active))
    return ValidatedConfig(resolved_defaults_path, defaults, tuple(valid_definitions), broken, qubes_vms or {})


def validate_startup_config(defaults_path: str | Path | None = None, *, check_qubes: bool = False) -> ValidatedConfig:
    # Strict callers retain the original all-valid contract on top of the shared inspection pipeline
    snapshot = inspect_startup_config(defaults_path, check_qubes=check_qubes)
    if snapshot.broken_kuhbs:
        raise ConfigValidationError(
            issue
            for broken in snapshot.broken_kuhbs
            for issue in broken.issues
        )
    return snapshot
