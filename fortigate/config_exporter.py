"""Fetch FortiGate cmdb config sections and write them out as JSON.

Built on top of :class:`fortigate.client.FortiGateClient`: given a list of
cmdb paths (e.g. ``"cmdb/firewall/policy"``), this discovers every VDOM on
the appliance, fetches each path in each VDOM, and writes the full API
response envelope as pretty-printed JSON under an output directory.

A single section failing to fetch (e.g. unsupported on this model/version)
does not abort the rest of the export -- see :func:`export_sections`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .client import FortiGateAPIError, FortiGateClient

__all__ = [
    "SectionFetchResult",
    "WrittenSection",
    "FailedSection",
    "ExportResult",
    "discover_vdoms",
    "build_export_plan",
    "fetch_section",
    "section_file_path",
    "write_section",
    "export_sections",
]


@dataclass
class SectionFetchResult:
    """The outcome of one :func:`fetch_section` call."""

    vdom: str
    path: str
    data: Any = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class WrittenSection:
    """A section that was successfully fetched and written to disk."""

    vdom: str
    path: str
    file_path: Path


@dataclass(frozen=True)
class FailedSection:
    """A section that failed to fetch."""

    vdom: str
    path: str
    error: str


@dataclass
class ExportResult:
    """Summary of an :func:`export_sections` run."""

    written: List[WrittenSection]
    failures: List[FailedSection]


def discover_vdoms(client: FortiGateClient) -> List[str]:
    """Return the names of every VDOM configured on the appliance.

    Works whether or not multi-VDOM mode is enabled -- in single-VDOM mode
    this simply returns ``["root"]``.
    """
    response = client.get("cmdb/system/vdom")
    results = response.get("results", []) if isinstance(response, dict) else []
    return [item["name"] for item in results if isinstance(item, dict) and "name" in item]


def build_export_plan(vdoms: List[str], paths: List[str]) -> List[Tuple[str, str]]:
    """Pair every path with every VDOM, in VDOM-major order."""
    return [(vdom, path) for vdom in vdoms for path in paths]


def fetch_section(client: FortiGateClient, vdom: str, path: str) -> SectionFetchResult:
    """Fetch one cmdb path in one VDOM, never raising.

    :raises: never -- a :class:`~fortigate.client.FortiGateAPIError` is
        caught and returned as ``SectionFetchResult.error`` instead.
    """
    try:
        data = client.get(path, vdom=vdom)
    except FortiGateAPIError as exc:
        return SectionFetchResult(vdom=vdom, path=path, error=str(exc))
    return SectionFetchResult(vdom=vdom, path=path, data=data)


def section_file_path(output_dir: Path, host_name: str, vdom: str, path: str) -> Path:
    """Compute where a fetched section should be written.

    Example: ``section_file_path(Path("data/hosts"), "fw1", "root",
    "cmdb/firewall/address")`` -> ``data/hosts/fw1/root/firewall-address.json``.
    """
    filename = path.strip("/").replace("/", "-") + ".json"
    return Path(output_dir) / host_name / vdom / filename


def write_section(file_path: Path, data: Any) -> None:
    """Write the full API response envelope to ``file_path`` as pretty JSON."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2))


def export_sections(
    client: FortiGateClient,
    paths: List[str],
    output_dir: Path,
    host_name: str,
) -> ExportResult:
    """Fetch ``paths`` across every VDOM on the appliance and write each to disk.

    A section failing to fetch is recorded in the returned
    :class:`ExportResult` rather than raising -- the rest of the export
    still proceeds.
    """
    vdoms = discover_vdoms(client)
    print(vdoms)
    plan = build_export_plan(vdoms, paths)

    written: List[WrittenSection] = []
    failures: List[FailedSection] = []
    for vdom, path in plan:
        result = fetch_section(client, vdom, path)
        if result.success:
            file_path = section_file_path(Path(output_dir), host_name, vdom, path)
            write_section(file_path, result.data)
            written.append(WrittenSection(vdom=vdom, path=path, file_path=file_path))
        else:
            failures.append(FailedSection(vdom=vdom, path=path, error=result.error))

    return ExportResult(written=written, failures=failures)
