"""Turn raw cmdb JSON exports into one diff-friendly YAML file per firewall.

The API responses written by :mod:`fortigate.config.exporter` are faithful
but hostile to comparison: every object carries a ``q_origin_key`` duplicate
of its own identity, tables come back as lists (so inserting one policy
shifts every following line of a diff), and per-request metadata like
``revision`` changes on every export even when nothing was configured.

This module rewrites that into a single mapping per host::

    host: fw1
    version: v7.4.12
    global:
      system/ntp:
        ntpsync: enable
    vdoms:
      root:
        firewall/policy:
          4:
            name: wan-dmz-rproxy
            srcintf: [wan1]

``global`` mirrors the appliance's own split in multi-VDOM mode: config
that exists once for the whole box rather than per-VDOM. It is a sibling
of ``vdoms`` rather than an entry inside it, so consumers iterating VDOMs
do not mistake it for one.

Tables become mappings keyed by their mkey, so a diff stays local to the
object that actually changed. Ordering within a table is whatever the API
returned -- for order-sensitive tables like ``firewall/policy`` that is the
firewall's real evaluation order, and it is preserved on write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .sections import GLOBAL_SCOPE

__all__ = [
    "HostFacts",
    "NormalizedHost",
    "section_path_from_filename",
    "extract_facts",
    "infer_mkey",
    "normalize_value",
    "normalize_section",
    "discover_scope_dirs",
    "normalize_host",
    "host_file_path",
    "write_normalized",
]

#: Per-object fields describing the firewall's own bookkeeping rather than
#: anything an operator configured.
NOISE_FIELDS = frozenset({"q_origin_key", "uuid", "uuid-idx"})

#: Envelope fields carrying facts about the appliance itself. These are
#: identical in every response, so they are lifted to the top of the file
#: instead of being repeated per section.
FACT_FIELDS = ("serial", "version", "build")


@dataclass(frozen=True)
class HostFacts:
    """Appliance-level facts, identical across every exported section."""

    serial: Optional[str] = None
    version: Optional[str] = None
    build: Optional[int] = None


@dataclass
class NormalizedHost:
    """A whole firewall's configuration, normalized."""

    host: str
    facts: HostFacts = field(default_factory=HostFacts)
    #: Whole-appliance config. Named ``global_config`` because ``global``
    #: is a reserved word; it is written out under the key ``global``.
    global_config: Dict[str, Any] = field(default_factory=dict)
    vdoms: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_mapping(self) -> Dict[str, Any]:
        """Render to the plain dict that gets written as YAML."""
        mapping: Dict[str, Any] = {"host": self.host}
        for name in FACT_FIELDS:
            value = getattr(self.facts, name)
            if value is not None:
                mapping[name] = value
        # Omitted entirely when nothing global was exported, so a
        # single-VDOM host's file keeps its previous shape.
        if self.global_config:
            mapping["global"] = self.global_config
        mapping["vdoms"] = self.vdoms
        return mapping


def section_path_from_filename(filename: str) -> str:
    """Recover the cmdb path a raw export file was written from.

    Example: ``"cmdb-firewall-policy.json"`` -> ``"firewall/policy"``. The
    ``cmdb`` prefix is dropped since every exported section shares it.
    """
    parts = Path(filename).stem.split("-")
    if parts and parts[0] == "cmdb":
        parts = parts[1:]
    return "/".join(parts)


def extract_facts(payload: Any) -> HostFacts:
    """Pull the appliance facts out of one API response envelope."""
    if not isinstance(payload, dict):
        return HostFacts()
    return HostFacts(**{name: payload.get(name) for name in FACT_FIELDS})


def infer_mkey(rows: List[Any]) -> Optional[str]:
    """Return the field ``rows`` are keyed by, or ``None`` if not a table.

    FortiGate echoes each object's identity in ``q_origin_key``, so the mkey
    is whichever field holds that same value -- no per-path lookup table and
    no schema call needed. Every row must agree, otherwise the list is left
    alone rather than guessed at.
    """
    candidates: Optional[set] = None
    for row in rows:
        if not isinstance(row, dict) or "q_origin_key" not in row:
            return None
        identity = row["q_origin_key"]
        matching = {
            name
            for name, value in row.items()
            if name != "q_origin_key" and value == identity
        }
        candidates = matching if candidates is None else candidates & matching
        if not candidates:
            return None
    if not candidates:
        return None
    # `name` is by far the most common mkey; prefer it when a row happens to
    # carry another field with the same value.
    return "name" if "name" in candidates else sorted(candidates)[0]


def _is_reference_list(rows: List[Any]) -> bool:
    """True if ``rows`` is a list of pure pointers, e.g. ``srcintf``.

    These arrive as ``[{"name": "wan1", "q_origin_key": "wan1"}]`` and carry
    no information beyond the name, so they collapse to ``["wan1"]``.
    """
    return all(
        isinstance(row, dict) and set(row) <= {"name", "q_origin_key"} and "name" in row
        for row in rows
    )


def normalize_value(value: Any) -> Any:
    """Recursively normalize one value from an API response.

    Nested tables get the same treatment as top-level ones -- ``ntpserver``
    inside ``system/ntp`` is keyed by ``id`` exactly as ``firewall/address``
    is keyed by ``name``.
    """
    if isinstance(value, dict):
        return {
            key: normalize_value(item)
            for key, item in value.items()
            if key not in NOISE_FIELDS
        }
    if not isinstance(value, list) or not value:
        return value
    if _is_reference_list(value):
        return [row["name"] for row in value]
    mkey = infer_mkey(value)
    if mkey is None:
        return [normalize_value(row) for row in value]
    return {
        row[mkey]: normalize_value(
            {key: item for key, item in row.items() if key != mkey}
        )
        for row in value
    }


def normalize_section(payload: Any) -> Any:
    """Normalize one raw export file's contents, envelope and all."""
    results = payload.get("results") if isinstance(payload, dict) else payload
    return normalize_value(results)


def discover_scope_dirs(host_dir: Path) -> List[Path]:
    """Return the scope directories present under ``host_dir``.

    These are one per VDOM plus, when global sections were exported, the
    :data:`GLOBAL_SCOPE` directory. Telling them apart is left to
    :func:`normalize_host`.
    """
    if not host_dir.is_dir():
        return []
    return sorted(child for child in host_dir.iterdir() if child.is_dir())


def normalize_host(raw_dir: Path, host_name: str) -> NormalizedHost:
    """Normalize every exported section for one firewall.

    Scopes and sections are read from whatever is on disk rather than from a
    caller-supplied list, so this stays correct as the export grows.
    """
    host_dir = Path(raw_dir) / host_name
    result = NormalizedHost(host=host_name)

    for scope_dir in discover_scope_dirs(host_dir):
        sections: Dict[str, Any] = {}
        for json_file in sorted(scope_dir.glob("*.json")):
            payload = json.loads(json_file.read_text())
            if result.facts == HostFacts():
                result.facts = extract_facts(payload)
            sections[section_path_from_filename(json_file.name)] = normalize_section(
                payload
            )
        if scope_dir.name == GLOBAL_SCOPE:
            result.global_config = sections
        else:
            result.vdoms[scope_dir.name] = sections

    return result


def host_file_path(output_dir: Path, host_name: str) -> Path:
    """Compute where a normalized host file should be written."""
    return Path(output_dir) / f"{host_name}.yaml"


def write_normalized(file_path: Path, host: NormalizedHost) -> None:
    """Write ``host`` to ``file_path`` as YAML.

    ``sort_keys=False`` is load-bearing: it keeps tables in the order the
    API returned them, which for ``firewall/policy`` is the firewall's
    evaluation order and therefore part of the configuration's meaning.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        yaml.safe_dump(host.to_mapping(), sort_keys=False, default_flow_style=False)
    )
