#!/usr/bin/env python3
"""Showcase: check fw1's normalized config against its desired state.

Run from anywhere::

    python3 scripts/check_fw1.py

Reads ``data/desired/fw1.yaml`` and ``data/normal/fw1.yaml`` and writes
``data/diff/fw1.yaml``. Requires ``merge_fw1.py`` and ``normalize_fw1.py``
to have run first; the two are reported separately, because collapsing
them would point at the wrong stage half the time. No template is
rendered here and Jinja2 is not in this path at all.

Stdout only says what happened and where to look; the detail lives in the
diff file.

Exit codes: 1 on any FAIL or MISSING, 0 on UNKNOWN alone. An unexported
path is a gap in stage 1, and ``export_fw1.py`` already fails its own run
loudly for it, naming the vdom, path, and API error -- re-reporting it
here would just be a worse error message. The UNKNOWN count in the
summary stays as the local tripwire.

A template fault still reaches this script: a stale or hand-edited
desired file fails validation on load, and a template expecting a mapping
where the firewall holds a scalar can only be seen at comparison time.
Both name a coordinate in the desired file, so both are reported against
it, and both abort before a diff is written -- a fault in the rules must
not leave a file that reads as a statement about the firewall.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.compliance.checker import (
    check_template,
    diff_file_path,
    load_normalized,
    write_diff,
)
from fortigate.compliance.template import (
    TemplateError,
    desired_file_path,
    load_document,
)
from fortigate.config.normalizer import host_file_path

HOST_NAME = "fw1"
# Anchored to the repo, not the working directory, so the script behaves
# the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
DESIRED_DIR = REPO_ROOT / "data" / "desired"
NORMAL_DIR = REPO_ROOT / "data" / "normal"
OUTPUT_DIR = REPO_ROOT / "data" / "diff"


def main() -> int:
    desired_file = desired_file_path(DESIRED_DIR, HOST_NAME)
    normal_file = host_file_path(NORMAL_DIR, HOST_NAME)

    try:
        try:
            desired = load_document(desired_file)
        except FileNotFoundError:
            print(
                f"no desired state at {desired_file}; "
                f"run scripts/merge_fw1.py first",
                file=sys.stderr,
            )
            return 1

        # Never normalized is not a compliance outcome: there is nothing
        # to be compliant or non-compliant about, and writing an empty
        # diff would make it look checked.
        try:
            host = load_normalized(normal_file)
        except FileNotFoundError:
            print(
                f"no normalized config at {normal_file}; "
                f"run scripts/normalize_fw1.py first",
                file=sys.stderr,
            )
            return 1

        result = check_template(desired, host)
    except TemplateError as error:
        print(f"{desired_file}: {error}", file=sys.stderr)
        return 1

    file_path = diff_file_path(OUTPUT_DIR, HOST_NAME)
    write_diff(file_path, result)

    print(
        f"{HOST_NAME}: FAIL {len(result.violations)}, "
        f"MISSING {len(result.missing)}, UNKNOWN {len(result.unknown)} "
        f"-> {file_path}"
    )
    return 0 if result.is_compliant else 1


if __name__ == "__main__":
    raise SystemExit(main())
