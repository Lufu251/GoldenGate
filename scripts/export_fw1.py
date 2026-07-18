#!/usr/bin/env python3
"""Showcase: export fw1's declared config sections.

Run from anywhere::

    python3 scripts/export_fw1.py

Which sections to fetch, and the scope each lives in, is declared in
``configuration/sections.yaml``.

Writes JSON files under ``data/raw/fw1/<scope>/<section>.json``, where
``<scope>`` is ``global`` or a VDOM name.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.api.client import FortiGateClient
from fortigate.api.inventory import Inventory
from fortigate.config.exporter import export_sections
from fortigate.config.sections import Sections

logger = logging.getLogger(__name__)

HOST_NAME = "fw1"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "raw"
SECTIONS_FILE = REPO_ROOT / "configuration" / "sections.yaml"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    entry = Inventory.load().get(HOST_NAME)
    sections = Sections.load(SECTIONS_FILE)
    with FortiGateClient.from_entry(entry) as fg:
        result = export_sections(fg, sections, OUTPUT_DIR, HOST_NAME)

    for written in result.written:
        print(f"wrote  {written.vdom}/{written.path} -> {written.file_path}")
    for failure in result.failures:
        print(f"failed {failure.vdom}/{failure.path}: {failure.error}")

    print(f"\n{len(result.written)} written, {len(result.failures)} failed")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
