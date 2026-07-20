#!/usr/bin/env python3
"""Showcase: export fw1's declared config sections.

Run from anywhere::

    python3 scripts/export_fw1.py

Which sections to fetch, and the scope each lives in, is declared in
``configuration/sections.yaml``.

Writes JSON files under ``data/raw/fw1/<scope>/<cmdb path>.json``, where
``<scope>`` is ``global`` or a VDOM name. The host's directory is replaced
on each run, so it never accumulates sections that are no longer declared.

The fetching is :mod:`fortigate.config.exporter`'s job; naming the files,
clearing the previous export, and writing are this script's.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path, PurePath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.api.client import FortiGateClient
from fortigate.api.inventory import Inventory
from fortigate.config.exporter import export_sections
from fortigate.config.sections import Sections

HOST_NAME = "fw1"
# Everything is anchored to the repo, not to the working directory, so the
# script behaves the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "raw"
SECTIONS_FILE = REPO_ROOT / "configuration" / "sections.yaml"
INVENTORY_FILE = REPO_ROOT / "inventory.yaml"


def section_filename(path: str) -> PurePath:
    """A cmdb path as a relative file path.

    ``cmdb/firewall/ssl-ssh-profile`` -> ``cmdb/firewall/ssl-ssh-profile.json``

    Nested rather than flattened into a single name: joining the segments
    with ``-`` is not reversible, because cmdb path segments contain ``-``
    themselves, and ``system/dns-database`` would read back as
    ``system/dns/database``. The normalizer recovers the path from this
    layout, so the two have to agree.
    """
    return PurePath(path.strip("/") + ".json")


def write_section(file_path: Path, data: Any) -> None:
    """Write one API response envelope to ``file_path`` as pretty JSON."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2))


def clear_host_dir(host_dir: Path) -> None:
    """Delete a host's export directory, if it exists.

    Only the sections fetched on this run are written, so without this a
    section dropped from ``sections.yaml`` -- or a VDOM deleted from the
    appliance -- would leave its JSON behind forever, and the normalizer
    reads whatever is on disk.
    """
    if host_dir.is_dir():
        shutil.rmtree(host_dir)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    entry = Inventory.load(INVENTORY_FILE).get(HOST_NAME)
    sections = Sections.load(SECTIONS_FILE)
    with FortiGateClient.from_entry(entry) as fg:
        result = export_sections(fg, sections)

    # Only reached once the appliance has answered, so an unreachable
    # firewall cannot destroy a good export. Nothing enforces that ordering
    # any more -- there is simply nothing to write until export_sections
    # has returned.
    host_dir = OUTPUT_DIR / HOST_NAME
    clear_host_dir(host_dir)

    for fetched in result.fetched:
        file_path = host_dir / fetched.vdom / section_filename(fetched.path)
        write_section(file_path, fetched.data)
        print(f"wrote  {fetched.vdom}/{fetched.path} -> {file_path}")

    for failure in result.failures:
        print(f"failed {failure.vdom}/{failure.path}: {failure.error}")

    print(f"\n{len(result.fetched)} written, {len(result.failures)} failed")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
