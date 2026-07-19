"""Fetch FortiGate cmdb config sections and write them out as JSON.

Built on top of :class:`fortigate.api.client.FortiGateClient`: given
:class:`~fortigate.config.sections.Section` values (a cmdb path such as
``"cmdb/firewall/policy"`` plus the scope it lives in), this discovers every
VDOM on the appliance, fetches each path in the scope it actually lives in,
and writes the full API response envelope as pretty-printed JSON under an
output directory.

In multi-VDOM mode a FortiGate splits its config into two scopes. Most
tables (``firewall/*``, ``router/*``, ``system/settings``) are per-VDOM,
but some (``system/global``, ``system/ntp``) live once in the *global*
scope and are reached with a ``global=1`` query parameter instead of a
``vdom=`` one. Which paths belong to which scope is FortiOS schema
knowledge that cannot be discovered from the appliance, so the caller
declares it by tagging each section with its scope. What that means for
fetching -- global once, per-VDOM once per VDOM -- is this module's
business, and :func:`build_export_plan` is where it happens.

A single section failing to fetch (e.g. unsupported on this model/version)
does not abort the rest of the export -- see :func:`export_sections`.

An export replaces a host's output directory wholesale rather than merging
into it, so sections that are no longer declared do not linger -- see
:func:`clear_host_dir`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from ..api.client import FortiGateAPIError, FortiGateClient
from .sections import GLOBAL_SCOPE, VDOM_SCOPE, Section

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
    "clear_host_dir",
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


def build_export_plan(
    vdoms: List[str],
    sections: Iterable[Section],
) -> List[Tuple[str, str]]:
    """Pair each section's path with the scope it should be fetched in.

    Global sections are emitted once under :data:`GLOBAL_SCOPE`; VDOM
    sections are crossed with every VDOM, in VDOM-major order.
    """
    sections = list(sections)  # may be a one-shot iterable; walked twice below
    plan = [(GLOBAL_SCOPE, s.path) for s in sections if s.scope == GLOBAL_SCOPE]
    plan += [
        (vdom, s.path) for vdom in vdoms for s in sections if s.scope == VDOM_SCOPE
    ]
    return plan


def fetch_section(client: FortiGateClient, vdom: str, path: str) -> SectionFetchResult:
    """Fetch one cmdb path in one scope, never raising.

    ``vdom`` is either a real VDOM name or :data:`GLOBAL_SCOPE`, in which
    case the path is read from the global scope instead.

    :raises: never -- a :class:`~fortigate.api.client.FortiGateAPIError` is
        caught and returned as ``SectionFetchResult.error`` instead.
    """
    try:
        if vdom == GLOBAL_SCOPE:
            data = client.get(path, params={"global": "1"})
        else:
            data = client.get(path, vdom=vdom)
    except FortiGateAPIError as exc:
        return SectionFetchResult(vdom=vdom, path=path, error=str(exc))
    return SectionFetchResult(vdom=vdom, path=path, data=data)


def section_file_path(output_dir: Path, host_name: str, vdom: str, path: str) -> Path:
    """Compute where a fetched section should be written.

    Example: ``section_file_path(Path("data/raw"), "fw1", "root",
    "cmdb/firewall/address")`` -> ``data/raw/fw1/root/cmdb-firewall-address.json``.

    Global sections land in a sibling ``global/`` directory, since
    :data:`GLOBAL_SCOPE` is passed as ``vdom``.
    """
    filename = path.strip("/").replace("/", "-") + ".json"
    return Path(output_dir) / host_name / vdom / filename


def write_section(file_path: Path, data: Any) -> None:
    """Write the full API response envelope to ``file_path`` as pretty JSON."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2))


def clear_host_dir(output_dir: Path, host_name: str) -> None:
    """Delete a host's export directory, if it exists.

    The export only ever writes the sections it is asked for, so without
    this a section dropped from the declaration -- or a VDOM deleted from
    the appliance -- would leave its JSON behind forever, and
    :mod:`fortigate.config.normalizer` reads whatever is on disk. Stale
    config would keep showing up in the normalized output indefinitely.

    Only ever called once the appliance has answered (see
    :func:`export_sections`), so an unreachable firewall cannot destroy a
    good export.
    """
    host_dir = Path(output_dir) / host_name
    if host_dir.is_dir():
        shutil.rmtree(host_dir)


def export_sections(
    client: FortiGateClient,
    sections: Iterable[Section],
    output_dir: Path,
    host_name: str,
) -> ExportResult:
    """Fetch every section in its own scope and write each result to disk.

    Sections scoped to :data:`VDOM_SCOPE` are fetched once per discovered
    VDOM; those scoped to :data:`GLOBAL_SCOPE` are fetched once from the
    appliance's global scope.

    A section failing to fetch is recorded in the returned
    :class:`ExportResult` rather than raising -- the rest of the export
    still proceeds.

    The host's existing export directory is deleted first, so the result is
    exactly what was declared and fetched on this run rather than an
    accumulation of every section ever exported. VDOM discovery happens
    before the delete so an unreachable appliance leaves the previous
    export intact.
    """
    vdoms = discover_vdoms(client)
    clear_host_dir(Path(output_dir), host_name)
    plan = build_export_plan(vdoms, sections)

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
