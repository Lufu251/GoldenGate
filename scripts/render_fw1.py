#!/usr/bin/env python3
"""Showcase: render fw1's templates into one document each.

Run from anywhere::

    python3 scripts/render_fw1.py

Reads ``vars/fw1.yaml`` and every template named in :data:`TEMPLATES`,
and writes ``data/rendered/fw1/<name>.yaml`` -- the same schema as the
template it came from, with the variables resolved and no Jinja2 left.
Needs no appliance and no earlier stage: its inputs are git-tracked repo
contents, so a missing one raises rather than being reported as "run X
first".

Exit codes: 1 on a template fault, naming the template that caused it, 0
once every template was written.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate.compliance.template import (
    TemplateError,
    load_template,
    load_vars,
    render,
    rendered_file_path,
    write_document,
)

HOST_NAME = "fw1"
# Which templates apply to this host. A constant while each script names
# one host; the inventory entry is where this eventually lives, and only
# the source of the list changes when it does.
TEMPLATES = ("baseline", "branch-office")
# Anchored to the repo, not the working directory, so the script behaves
# the same wherever it is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"
VARS_FILE = REPO_ROOT / "vars" / f"{HOST_NAME}.yaml"
OUTPUT_DIR = REPO_ROOT / "data" / "rendered"


def clear_host_dir(host_dir: Path) -> None:
    """Delete a host's rendered directory, if it exists.

    Only the templates rendered on this run are written, so without this
    a template dropped from :data:`TEMPLATES` would leave its file
    behind and the merge stage would keep folding it in -- it globs the
    directory rather than re-reading the list.
    """
    if host_dir.is_dir():
        shutil.rmtree(host_dir)


def main() -> int:
    variables = load_vars(VARS_FILE)

    host_dir = OUTPUT_DIR / HOST_NAME
    clear_host_dir(host_dir)

    for name in TEMPLATES:
        try:
            document = render(load_template(TEMPLATE_DIR / f"{name}.yaml"), variables)
            # Validates before writing, so an unsatisfiable assertion
            # never reaches disk for the merge stage to fold in.
            write_document(rendered_file_path(OUTPUT_DIR, HOST_NAME, name), document)
        except TemplateError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
        print(f"wrote {rendered_file_path(OUTPUT_DIR, HOST_NAME, name)}")

    print(f"\n{len(TEMPLATES)} template(s) rendered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
