#!/usr/bin/env python3
"""Showcase: normalize fw1's raw export into a single YAML file.

Run from anywhere::

    python3 scripts/normalize_fw1.py

Reads ``data/raw/fw1/<scope>/*.json`` and writes ``data/normal/fw1.yaml``.
Requires ``export_fw1.py`` to have run first.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.config.normalizer import host_file_path, normalize_host, write_normalized

HOST_NAME = "fw1"
# Anchored to the repo, not the working directory, so the script behaves
# the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
OUTPUT_DIR = REPO_ROOT / "data" / "normal"


def main() -> int:
    host = normalize_host(RAW_DIR, HOST_NAME)

    # Nothing on disk normalizes to an empty-but-valid host, which would
    # overwrite a good fw1.yaml with a file claiming the firewall has no
    # config at all. Bail out instead of writing it.
    if not host.vdoms and not host.global_config:
        print(
            f"no raw export found under {RAW_DIR / HOST_NAME}; "
            f"run scripts/export_fw1.py first",
            file=sys.stderr,
        )
        return 1

    file_path = host_file_path(OUTPUT_DIR, HOST_NAME)
    write_normalized(file_path, host)

    sections = len(host.global_config) + sum(
        len(sections) for sections in host.vdoms.values()
    )
    print(f"wrote {file_path}")
    print(
        f"{len(host.vdoms)} vdom(s) + global, {sections} section(s)"
        if host.global_config
        else f"{len(host.vdoms)} vdom(s), {sections} section(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
