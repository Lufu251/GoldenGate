#!/usr/bin/env python3
"""Showcase: export a handful of config sections from fw1.

Run from the repo root::

    python3 scripts/export_fw1.py

Writes JSON files under ``data/hosts/fw1/<vdom>/<section>.json``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.client import FortiGateClient
from fortigate.config_exporter import export_sections
from fortigate.inventory import get_entry

logger = logging.getLogger(__name__)

HOST_NAME = "fw1"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
SECTIONS = [
    "cmdb/system/global",
    "cmdb/system/ntp",
    "cmdb/firewall/address",
    "cmdb/firewall/policy",
]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    entry = get_entry(HOST_NAME)
    with FortiGateClient.from_entry(entry) as fg:
        result = export_sections(fg, SECTIONS, OUTPUT_DIR, HOST_NAME)

    for written in result.written:
        print(f"wrote  {written.vdom}/{written.path} -> {written.file_path}")
    for failure in result.failures:
        print(f"failed {failure.vdom}/{failure.path}: {failure.error}")

    print(f"\n{len(result.written)} written, {len(result.failures)} failed")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
