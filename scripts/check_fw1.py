#!/usr/bin/env python3
"""Showcase: check fw1's normalized config against the baseline template.

Run from anywhere::

    python3 scripts/check_fw1.py

Reads ``vars/fw1.yaml`` and ``templates/baseline.yaml``, renders one
against the other, checks the result against ``data/normal/fw1.yaml``,
and writes ``data/diff/fw1.yaml``. Requires ``normalize_fw1.py`` to have
run first.

Stdout only says what happened and where to look; the detail lives in the
diff file.

Exit codes: 1 on any FAIL or MISSING, 0 on UNKNOWN alone. An unexported
path is a gap in stage 1, and ``export_fw1.py`` already fails its own run
loudly for it, naming the vdom, path, and API error -- re-reporting it
here would just be a worse error message. The UNKNOWN count in the
summary stays as the local tripwire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.compliance.checker import (
    check_template,
    diff_file_path,
    load_vars,
    render_template,
    write_diff,
)

HOST_NAME = "fw1"
# Anchored to the repo, not the working directory, so the script behaves
# the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
NORMAL_DIR = REPO_ROOT / "data" / "normal"
OUTPUT_DIR = REPO_ROOT / "data" / "diff"
TEMPLATE_FILE = REPO_ROOT / "templates" / "baseline.yaml"
VARS_FILE = REPO_ROOT / "vars" / f"{HOST_NAME}.yaml"


def main() -> int:
    template = render_template(TEMPLATE_FILE, load_vars(VARS_FILE))

    # Never normalized is not a compliance outcome: there is nothing to be
    # compliant or non-compliant about, and writing an empty diff would
    # make it look checked.
    try:
        result = check_template(template, NORMAL_DIR, HOST_NAME)
    except FileNotFoundError:
        print(
            f"no normalized config for {HOST_NAME} under {NORMAL_DIR}; "
            f"run scripts/normalize_fw1.py first",
            file=sys.stderr,
        )
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
