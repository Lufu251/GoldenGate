#!/usr/bin/env python3
"""Showcase: normalize fw1's raw export into a single YAML file.

Run from the repo root::

    python3 scripts/normalize_fw1.py

Reads ``data/raw/fw1/<vdom>/*.json`` and writes ``data/normal/fw1.yaml``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.normalizer import host_file_path, normalize_host, write_normalized

HOST_NAME = "fw1"
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
OUTPUT_DIR = REPO_ROOT / "data" / "normal"


def main() -> int:
    host = normalize_host(RAW_DIR, HOST_NAME)
    file_path = host_file_path(OUTPUT_DIR, HOST_NAME)
    write_normalized(file_path, host)

    sections = sum(len(sections) for sections in host.vdoms.values())
    print(f"wrote {file_path}")
    print(f"{len(host.vdoms)} vdom(s), {sections} section(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
