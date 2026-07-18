"""Which config sections to export, and the scope each one lives in.

Reads ``configuration/sections.yaml``, a YAML mapping of scope to cmdb
paths::

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

The file groups paths by scope because that reads well; in memory the
grouping is flattened to a single list of :class:`Section`, each carrying
its own scope. Consumers that care about the distinction -- really only
:mod:`fortigate.config.exporter` -- filter for themselves, rather than
having the split handed to them pre-made and having to keep the two
halves paired up.

Loading it is deliberately kept out of :mod:`fortigate.config.exporter`,
exactly as :mod:`fortigate.api.inventory` is kept out of
:mod:`fortigate.api.client`: scripts load the declaration and pass the
sections in, so the exporter stays usable with any list of sections and
never requires a file on disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, List, NamedTuple, Union

import yaml

__all__ = [
    "GLOBAL_SCOPE",
    "VDOM_SCOPE",
    "SCOPES",
    "Section",
    "Sections",
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


class Section(NamedTuple):
    """One cmdb path together with the scope it lives in.

    :param path: The cmdb path, e.g. ``"cmdb/firewall/policy"``.
    :param scope: :data:`GLOBAL_SCOPE` or :data:`VDOM_SCOPE`.
    """

    path: str
    scope: str


class Sections:
    """The cmdb paths to export, each tagged with its scope.

    Build one with :meth:`load`; iterate it to get :class:`Section` values
    in declaration order (global first, then vdom).
    """

    def __init__(self, sections: List[Section]) -> None:
        self._sections = list(sections)

    def __iter__(self) -> Iterator[Section]:
        return iter(self._sections)

    def __len__(self) -> int:
        return len(self._sections)

    def __repr__(self) -> str:
        return f"Sections({self._sections!r})"

    @classmethod
    def load(
        cls,
        path: Union[str, "os.PathLike[str]", None] = None,
    ) -> "Sections":
        """Load the section declaration from ``configuration/sections.yaml``.

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
                "no configuration/sections.yaml found; create one declaring "
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

        sections: List[Section] = []
        seen: dict[str, str] = {}
        for scope in SCOPES:
            value = raw.get(scope) or []
            if not isinstance(value, list):
                raise ValueError(f"{sections_path}: '{scope}' must be a list of paths")
            for i, item in enumerate(value):
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(
                        f"{sections_path}: '{scope}' entry {i} must be a non-empty "
                        f"string, e.g. 'cmdb/firewall/policy'"
                    )
                path_str = item.strip()
                # A path can only live in one scope; declaring it twice means
                # one of them is wrong, and silently picking either would
                # export the section to the wrong place.
                if path_str in seen:
                    where = (
                        f"twice under '{scope}'"
                        if seen[path_str] == scope
                        else f"under both '{seen[path_str]}' and '{scope}'"
                    )
                    raise ValueError(
                        f"{sections_path}: path '{path_str}' declared {where}"
                    )
                seen[path_str] = scope
                sections.append(Section(path=path_str, scope=scope))

        if not sections:
            raise ValueError(f"{sections_path}: declares no paths to export")
        return cls(sections)


def _find_sections() -> Union[Path, None]:
    """Search the current directory and its parents for the sections file.

    Looks for ``configuration/sections.yaml`` under each directory walked,
    so scripts find it whether run from the repo root or a subdirectory
    (e.g. ``scripts/``). Unlike ``inventory.yaml``, which holds secrets and
    sits at the root, this one is tracked in git under ``configuration/``.
    Returns ``None`` if none is found.
    """
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "configuration" / "sections.yaml"
        if candidate.is_file():
            return candidate
    return None
