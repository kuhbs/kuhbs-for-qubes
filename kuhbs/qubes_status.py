# Purpose: Share Qubes metadata parsing and cache live state for KUHBS status
# Scope: Static VM config from qubes.xml, live running/paused state from xl list
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

from .operations import OperationContext


QUBES_XML = Path("/var/lib/qubes/qubes.xml")


@dataclass(frozen=True)
class VmInfo:
    # Cache static Qubes metadata for one named VM
    name: str
    klass: str
    label: str
    template: str
    dispvm_template: str
    netvm: str


@dataclass
class QubesStatus:
    # Keep one per-refresh snapshot of Xen runtime state and static Qubes metadata
    ctx: OperationContext
    xml_path: Path = QUBES_XML
    vms: dict[str, VmInfo] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, ctx: OperationContext, xml_path: Path | None = None) -> "QubesStatus":
        # Load the runtime and XML snapshots once for this status pass
        status = cls(ctx=ctx, xml_path=xml_path or _xml_path(ctx))
        status.states = status._load_xl_states()
        status.vms = status.read_qubes_xml(status.xml_path)
        return status

    def _load_xl_states(self) -> dict[str, str]:
        # A failed authoritative Xen query aborts instead of pretending every VM is halted
        result = self.ctx.runner.run(["xl", "list"], log=False)

        states: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] == "Name":
                continue

            name = "dom0" if parts[0] == "Domain-0" else parts[0]
            if name.endswith("-dm"):
                continue

            flags = parts[4]
            if "p" in flags:
                states[name] = "paused"
            # A shutting-down domain remains in xl briefly but is no longer active for restart planning
            elif "s" in flags:
                states[name] = "shutdown"
            elif "c" in flags:
                states[name] = "crashed"
            elif "d" in flags:
                states[name] = "dying"
            else:
                states[name] = "running"

        return states

    @classmethod
    def read_qubes_xml(
        cls,
        path: Path = QUBES_XML,
    ) -> dict[str, VmInfo]:
        # Status-only callers tolerate an absent test fixture; strict validation checks its path first
        if not path.exists():
            return {}
        root = ET.parse(path).getroot()

        domains = root.find("domains")
        if domains is None:
            raise ValueError("Qubes configuration has no domains section")

        domain_elements = domains.findall("domain")
        domain_refs = cls._domain_ref_map(domain_elements)
        label_refs = cls._label_ref_map(root)

        root_props = cls._properties(root)
        root_refs = cls._property_refs(root)
        # Qubes can store VM-valued properties as object refs instead of visible names
        default_netvm = _clean_vm_name(_resolve_ref(root_refs.get("default_netvm", root_props.get("default_netvm", "")), domain_refs))

        vms: dict[str, VmInfo] = {}
        for domain in domain_elements:
            props = cls._properties(domain)
            refs = cls._property_refs(domain)
            present = cls._present_properties(domain)
            klass = domain.attrib.get("class", "")
            name = props.get("name") or ("dom0" if klass == "AdminVM" else "")
            if not name:
                continue

            if "netvm" in present:
                netvm = _clean_vm_name(_resolve_ref(refs.get("netvm", props.get("netvm", "")), domain_refs))
            elif klass in {"AppVM", "StandaloneVM", "DispVM"}:
                netvm = default_netvm
            else:
                netvm = ""

            label = _resolve_ref(refs.get("label", props.get("label", "")), label_refs).lower()
            template = _clean_vm_name(_resolve_ref(refs.get("template", props.get("template", "")), domain_refs))
            dispvm_template = _clean_vm_name(_resolve_ref(refs.get("dispvm_template", props.get("dispvm_template", "")), domain_refs))

            vms[name] = VmInfo(
                name=name,
                klass=klass,
                label=label,
                template=template,
                dispvm_template=dispvm_template,
                netvm=netvm,
            )
        return vms

    @classmethod
    def _domain_ref_map(cls, domains: list[ET.Element]) -> dict[str, str]:
        # Map qubes.xml domain IDs to VM names
        refs: dict[str, str] = {}
        for domain in domains:
            props = cls._properties(domain)
            klass = domain.attrib.get("class", "")
            name = props.get("name") or ("dom0" if klass == "AdminVM" else "")
            if not name:
                continue
            # Qubes object ids are stable inside qubes.xml and may be referenced by properties
            for attr in ("id", "ref"):
                if domain.attrib.get(attr):
                    refs[domain.attrib[attr]] = name
        return refs

    @staticmethod
    def _label_ref_map(root: ET.Element) -> dict[str, str]:
        # Map qubes.xml label IDs to label names
        labels = root.find("labels")
        if labels is None:
            return {}
        refs: dict[str, str] = {}
        for label in labels.findall("label"):
            name = label.attrib.get("name") or (label.text or "").strip()
            if not name:
                continue
            # Label refs use the same XML object-reference style as VM refs
            for attr in ("id", "ref"):
                if label.attrib.get(attr):
                    refs[label.attrib[attr]] = name
        return refs

    @staticmethod
    def _properties(element) -> dict[str, str]:
        # Read direct string properties from one XML element
        properties = element.find("properties")
        if properties is None:
            return {}
        return {
            prop.attrib["name"]: (prop.text or "").strip()
            for prop in properties.findall("property")
            if "name" in prop.attrib
        }

    @staticmethod
    def _property_refs(element) -> dict[str, str]:
        # Read reference-valued properties from one XML element
        properties = element.find("properties")
        if properties is None:
            return {}
        return {
            prop.attrib["name"]: prop.attrib["ref"]
            for prop in properties.findall("property")
            if "name" in prop.attrib and prop.attrib.get("ref")
        }

    @staticmethod
    def _present_properties(element) -> set[str]:
        # Track properties that are present even when their value is empty
        properties = element.find("properties")
        if properties is None:
            return set()
        return {prop.attrib["name"] for prop in properties.findall("property") if "name" in prop.attrib}

    def vm_info(self, name: str) -> VmInfo | None:
        # Return cached metadata for one VM
        return self.vms.get(name)

    def netvm_chain(self, name: str) -> tuple[str, ...]:
        # Follow cached netvm parents until the chain ends or repeats
        chain: list[str] = []
        seen = {name}
        current = self.vms[name].netvm if name in self.vms else ""
        while current:
            chain.append(current)
            if current in seen:
                break
            seen.add(current)
            current = self.vms[current].netvm if current in self.vms else ""
        return tuple(chain)

def read_qubes_xml(path: Path = QUBES_XML) -> dict[str, VmInfo]:
    # Desktop callers use the same static metadata parser as the core status cache
    return QubesStatus.read_qubes_xml(path)


def _clean_vm_name(value: str) -> str:
    # Normalize Qubes empty and None spellings to no VM
    value = value.strip()
    return "" if value in {"", "-", "None", "none"} else value


def _resolve_ref(value: str, refs: dict[str, str]) -> str:
    # Resolve an XML object reference while preserving literal values
    return refs.get(value, value)


def _xml_path(ctx: OperationContext) -> Path:
    # Use the configured test path or the real Qubes XML path
    configured = ctx.defaults.get("paths", {}).get("qubes_xml")
    return Path(configured) if configured else QUBES_XML
