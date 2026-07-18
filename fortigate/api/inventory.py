"""Firewall inventory loading.

Reads ``inventory.yaml``, a YAML list of every FortiGate this tooling
manages. Each entry carries its own connection details under a friendly
``name``, used to select a target and to group its exports, so scripts can
loop over a whole fleet instead of hard-coding one device.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Union

import yaml

__all__ = [
    "VerifyType",
    "DEFAULT_PORT",
    "DEFAULT_VERIFY",
    "FirewallEntry",
    "Inventory",
]

# TLS verification: bool, or a path to a CA bundle.
VerifyType = Union[bool, str]

DEFAULT_PORT = 443
DEFAULT_VERIFY: VerifyType = True


@dataclass(frozen=True)
class FirewallEntry:
    """One firewall's connection details, as loaded from the inventory.

    :param name: Friendly identifier for this firewall (e.g. ``"fw1"``).
        Used to select a target and to name its host export directory, e.g.
        ``data/hosts/<name>/<vdom>/<section>.json``.
    :param address: Host/IP to connect to.
    :param token: REST API token. Keep this secret -- ``inventory.yaml`` is
        gitignored for that reason.
    :param port: HTTPS port.
    :param verify: TLS verification -- ``True``/``False`` or a path to a CA
        bundle.
    """

    name: str
    address: str
    token: str
    port: int = DEFAULT_PORT
    verify: VerifyType = DEFAULT_VERIFY


class Inventory:
    """Every firewall this tooling manages, keyed by name.

    Build one with :meth:`load`; look a firewall up with :meth:`get`, or
    iterate to walk the whole fleet in file order.
    """

    def __init__(self, entries: Dict[str, FirewallEntry]) -> None:
        self._entries = dict(entries)

    def __iter__(self) -> Iterator[FirewallEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        # Entries carry API tokens, so list names only -- this must stay
        # safe to log.
        return f"Inventory({list(self._entries)!r})"

    def get(self, name: str) -> FirewallEntry:
        """Look up a single firewall by its inventory name.

        :raises KeyError: if no entry with that name exists.
        """
        try:
            return self._entries[name]
        except KeyError:
            raise KeyError(f"no inventory entry named '{name}'") from None

    @classmethod
    def load(
        cls,
        path: Union[str, "os.PathLike[str]", None] = None,
    ) -> "Inventory":
        """Load the firewall inventory from ``inventory.yaml``.

        When no ``path`` is given, the current directory and its parents are
        searched (see :func:`_find_inventory`).

        :raises FileNotFoundError: if no inventory file is found.
        :raises ValueError: if the file isn't a list of mappings, an entry is
            missing a required field (``name``, ``address``, ``token``), or a
            ``name`` is duplicated.
        """
        inv_path = Path(path) if path is not None else _find_inventory()
        if inv_path is None or not inv_path.is_file():
            raise FileNotFoundError(
                "no inventory.yaml found; create one at the repo root with your "
                "firewall entries (name, address, token)"
            )

        raw = yaml.safe_load(inv_path.read_text()) or []
        if not isinstance(raw, list):
            raise ValueError(f"{inv_path}: expected a YAML list of firewall entries")

        entries: Dict[str, FirewallEntry] = {}
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"{inv_path}: entry {i} is not a mapping")

            missing = [key for key in ("name", "address", "token") if not item.get(key)]
            if missing:
                raise ValueError(
                    f"{inv_path}: entry {i} missing required field(s): "
                    f"{', '.join(missing)}"
                )

            name = str(item["name"])
            # Names select a target and name the export directory, so a
            # duplicate would silently shadow a firewall and overwrite its
            # exports.
            if name in entries:
                raise ValueError(f"{inv_path}: duplicate inventory name '{name}'")

            entries[name] = FirewallEntry(
                name=name,
                address=str(item["address"]),
                token=str(item["token"]),
                port=int(item.get("port", DEFAULT_PORT)),
                verify=item.get("verify", DEFAULT_VERIFY),
            )
        return cls(entries)


def _find_inventory() -> Optional[Path]:
    """Search the current directory and its parents for ``inventory.yaml``.

    Lets scripts find it whether run from the repo root or a subdirectory
    (e.g. ``scripts/``). Returns ``None`` if none is found.
    """
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / "inventory.yaml"
        if candidate.is_file():
            return candidate
    return None
