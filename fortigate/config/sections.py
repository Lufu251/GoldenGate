"""Which config sections to export, and the scope each one lives in.

Reads ``sections.yaml``, a YAML mapping of scope to cmdb paths::

    global:
      - cmdb/system/global
    vdom:
      - cmdb/firewall/policy

In multi-VDOM mode a FortiGate splits its config in two. Most tables are
per-VDOM, but some exist once for the whole appliance and are read from
the *global* scope instead. Which is which is a property of the FortiOS
data model, not of any particular appliance, and it cannot be discovered
over the API -- a global table reads back normally from inside a VDOM, so
there is no negative signal to detect. It therefore has to be declared,
and this file is where.

Loading it is deliberately kept out of :mod:`fortigate.config.exporter`,
exactly as :mod:`fortigate.api.inventory` is kept out of
:mod:`fortigate.api.client`: scripts load the declaration and pass the paths
in, so the exporter stays usable with any list of paths and never
requires a file on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

import yaml

__all__ = [
    "GLOBAL_SCOPE",
    "VDOM_SCOPE",
    "SCOPES",
    "SectionSet",
    "load_sections",
]

#: Scope holding config that exists once for the whole appliance. Doubles
#: as the directory name such sections are exported to, and so as the key
#: they are normalized under -- see :mod:`fortigate.config.exporter` and
#: :mod:`fortigate.config.normalizer`, which both import it from here so the name
#: is defined in exactly one place.
GLOBAL_SCOPE = "global"

#: Scope holding config that exists separately in each VDOM.
VDOM_SCOPE = "vdom"

SCOPES = (GLOBAL_SCOPE, VDOM_SCOPE)


@dataclass(frozen=True)
class SectionSet:
    """The cmdb paths to export, grouped by the scope they live in.

    :param global_paths: Paths read once from the appliance's global scope.
    :param vdom_paths: Paths read once per VDOM.
    """

    global_paths: List[str] = field(default_factory=list)
    vdom_paths: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.global_paths) + len(self.vdom_paths)


def _find_sections() -> Union[Path, None]:
    """Search the current directory and its parents for ``sections.yaml``.

    Lets scripts find it whether run from the repo root or a subdirectory
    (e.g. ``scripts/``). Returns ``None`` if none is found.
    """
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "sections.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_sections(
    path: Union[str, "os.PathLike[str]", None] = None,
) -> SectionSet:
    """Load the section declaration from ``sections.yaml``.

    When no ``path`` is given, the current directory and its parents are
    searched (see :func:`_find_sections`).

    :raises FileNotFoundError: if no sections file is found.
    :raises ValueError: if the file isn't a mapping, names a scope other
        than ``global``/``vdom``, has a non-list of strings under a scope,
        declares the same path twice, or declares nothing at all.
    """
    sections_path = Path(path) if path is not None else _find_sections()
    if sections_path is None or not sections_path.is_file():
        raise FileNotFoundError(
            "no sections.yaml found; create one at the repo root declaring "
            "the cmdb paths to export under 'global:' and 'vdom:'"
        )

    raw = yaml.safe_load(sections_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{sections_path}: expected a YAML mapping of scope to paths, "
            f"e.g. 'global:' and 'vdom:'"
        )

    # Catch typos like 'vdoms:' rather than silently exporting nothing --
    # an empty scope is indistinguishable from a misspelled one.
    unknown = sorted(set(raw) - set(SCOPES))
    if unknown:
        raise ValueError(
            f"{sections_path}: unknown scope(s) {', '.join(unknown)}; "
            f"expected only {' and '.join(SCOPES)}"
        )

    by_scope = {}
    for scope in SCOPES:
        value = raw.get(scope) or []
        if not isinstance(value, list):
            raise ValueError(f"{sections_path}: '{scope}' must be a list of paths")
        paths = []
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"{sections_path}: '{scope}' entry {i} must be a non-empty "
                    f"string, e.g. 'cmdb/firewall/policy'"
                )
            paths.append(item.strip())
        by_scope[scope] = paths

    duplicates = sorted(
        path
        for path in set(by_scope[GLOBAL_SCOPE]) & set(by_scope[VDOM_SCOPE])
        # A path can only live in one scope; declaring both means one of
        # them is wrong, and silently picking either would export the
        # section to the wrong place.
    )
    if duplicates:
        raise ValueError(
            f"{sections_path}: path(s) declared in both scopes: "
            f"{', '.join(duplicates)}"
        )

    sections = SectionSet(
        global_paths=by_scope[GLOBAL_SCOPE],
        vdom_paths=by_scope[VDOM_SCOPE],
    )
    if not len(sections):
        raise ValueError(f"{sections_path}: declares no paths to export")
    return sections
