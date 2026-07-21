#!/usr/bin/env python3
"""Showcase: merge fw1's rendered templates into one desired state.

Run from anywhere::

    python3 scripts/merge_fw1.py

Reads every ``data/rendered/fw1/*.yaml`` and writes
``data/desired/fw1.yaml``. Requires ``render_fw1.py`` to have run first.

The directory is globbed rather than the template list re-read, so this
stage discovers what was actually rendered and cannot drift from the
list. Order affects only key order in the output and which conflict is
reported first.

Merging is optional; the stage is not. With a single rendered file the
document is written through unchanged -- ``{}`` is the identity for a
deep merge -- so ``check_fw1.py`` has exactly one input path either way.

Exit codes: 1 on a conflict between two templates or on nothing to
write, 0 once the desired state exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.compliance.template import (
    TemplateError,
    desired_file_path,
    load_document,
    merge,
    write_document,
)

HOST_NAME = "fw1"
# Anchored to the repo, not the working directory, so the script behaves
# the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERED_DIR = REPO_ROOT / "data" / "rendered"
OUTPUT_DIR = REPO_ROOT / "data" / "desired"


def main() -> int:
    host_dir = RENDERED_DIR / HOST_NAME
    rendered_files = sorted(host_dir.glob("*.yaml"))

    if not rendered_files:
        print(
            f"no rendered templates under {host_dir}; "
            f"run scripts/render_fw1.py first",
            file=sys.stderr,
        )
        return 1

    # An explicit loop rather than functools.reduce: reduce raises
    # TypeError on an empty list, where no templates at all is an ordinary
    # refusal to write, and it discards which document was being folded
    # in, so a conflict could not name the file that introduced it.
    merged: dict = {}
    for file_path in rendered_files:
        # Both a stale file that no longer validates and a disagreement
        # with what has been folded in so far arrive here, and the file
        # in hand is the one that introduced either.
        try:
            merged = merge(merged, load_document(file_path))
        except TemplateError as error:
            print(f"{file_path}: {error}", file=sys.stderr)
            return 1

    # Every template rendering to nothing would write a desired state
    # asserting nothing, against which every firewall is compliant. That
    # is "no policy was ever chosen for this host", not a clean bill of
    # health.
    if not merged:
        print(
            f"every template under {host_dir} rendered to nothing; "
            f"refusing to write a desired state that asserts nothing",
            file=sys.stderr,
        )
        return 1

    file_path = desired_file_path(OUTPUT_DIR, HOST_NAME)
    write_document(file_path, merged)

    print(f"wrote {file_path}")
    print(f"{len(rendered_files)} document(s) merged, {len(merged)} section(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
