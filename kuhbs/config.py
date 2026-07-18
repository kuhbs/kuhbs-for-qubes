# Purpose: YAML loading and defaults/definition path resolution
# Scope: Keep product schema validation in validation.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    # KUHBS config is reviewed by humans, so duplicate YAML keys must not silently overwrite earlier values
    pass


def _construct_mapping_without_duplicates(loader, node, deep=False):
    # Build a YAML mapping while rejecting duplicate human-edited keys
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_without_duplicates)


def resolve_path(value: str | Path) -> Path:
    # Expand only user-facing ~ paths so Qubes paths and shell snippets stay literal
    text = str(value)
    if text == "~" or text.startswith("~/"):
        return Path.home() / text[2:]
    return Path(text)


def load_yaml(path: str | Path) -> dict[str, Any]:
    # Load one YAML file and require the top-level value to be a mapping
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML file does not exist: {yaml_path}")
    if not yaml_path.is_file():
        raise ValueError(f"YAML path is not a file: {yaml_path}")
    try:
        data = yaml.load(yaml_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"YAML parse error in {yaml_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping: {yaml_path}")
    return data


def load_defaults(path: str | Path) -> dict[str, Any]:
    # Load packaged defaults through the same strict YAML loader as KUHB files
    return load_yaml(path)


def load_kuhb_definition(path: str | Path) -> dict[str, Any]:
    # Load one kuhb.yml through the same strict YAML loader as defaults
    return load_yaml(path)


def repo_defaults_path() -> Path:
    # Installed CLI/GUI must not trust a defaults.yml in the current working directory
    installed = Path("/usr/share/kuhbs/defaults.yml")
    if installed.exists():
        return installed
    raise FileNotFoundError(f"KUHBS defaults.yml not found: {installed}")
