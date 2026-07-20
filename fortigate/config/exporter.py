"""Fetch FortiGate cmdb config sections and return them.

Built on top of :class:`fortigate.api.client.FortiGateClient`: given
:class:`~fortigate.config.sections.Section` values (a cmdb path such as
``"cmdb/firewall/policy"`` plus the scope it lives in), this discovers every
VDOM on the appliance and fetches each path in the scope it actually lives
in.

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

Nothing here touches the filesystem. Where the fetched data lands, and
what naming it lands under, is the caller's decision. That also settles
*when* a previous export may be destroyed: there is nothing to write until
:func:`export_sections` has returned, so an unreachable appliance cannot
cost you the export you already had.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple, Union

from ..api.client import FortiGateAPIError, FortiGateClient
from .sections import GLOBAL_SCOPE, VDOM_SCOPE, Section

__all__ = [
    "FetchedSection",
    "FailedSection",
    "SectionResult",
    "ExportResult",
    "discover_vdoms",
    "build_export_plan",
    "fetch_section",
    "export_sections",
]


@dataclass(frozen=True)
class FetchedSection:
    """A section the appliance returned."""

    vdom: str
    path: str
    data: Any


@dataclass(frozen=True)
class FailedSection:
    """A section that could not be fetched."""

    vdom: str
    path: str
    error: str


#: The outcome of one :func:`fetch_section` call. Two types rather than one
#: carrying an optional ``data`` and an optional ``error``, so a section
#: that was never fetched cannot be constructed holding data.
SectionResult = Union[FetchedSection, FailedSection]


@dataclass
class ExportResult:
    """Summary of an :func:`export_sections` run."""

    fetched: List[FetchedSection]
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


def fetch_section(client: FortiGateClient, vdom: str, path: str) -> SectionResult:
    """Fetch one cmdb path in one scope, never raising.

    ``vdom`` is either a real VDOM name or :data:`GLOBAL_SCOPE`, in which
    case the path is read from the global scope instead.

    :raises: never -- a :class:`~fortigate.api.client.FortiGateAPIError` is
        caught and returned as a :class:`FailedSection` instead.
    """
    try:
        if vdom == GLOBAL_SCOPE:
            data = client.get(path, params={"global": "1"})
        else:
            data = client.get(path, vdom=vdom)
    except FortiGateAPIError as exc:
        return FailedSection(vdom=vdom, path=path, error=str(exc))
    return FetchedSection(vdom=vdom, path=path, data=data)


def export_sections(
    client: FortiGateClient,
    sections: Iterable[Section],
) -> ExportResult:
    """Fetch every section in its own scope and return what came back.

    Sections scoped to :data:`VDOM_SCOPE` are fetched once per discovered
    VDOM; those scoped to :data:`GLOBAL_SCOPE` are fetched once from the
    appliance's global scope.

    A section failing to fetch is recorded in the returned
    :class:`ExportResult` rather than raising -- the rest of the export
    still proceeds. VDOM discovery is not covered by that: failing to reach
    the appliance at all raises, because there is no partial result to
    report and the caller, not this function, decides whether one dead
    firewall stops a fleet.
    """
    fetched: List[FetchedSection] = []
    failures: List[FailedSection] = []

    for vdom, path in build_export_plan(discover_vdoms(client), sections):
        result = fetch_section(client, vdom, path)
        if isinstance(result, FetchedSection):
            fetched.append(result)
        else:
            failures.append(result)

    return ExportResult(fetched=fetched, failures=failures)
