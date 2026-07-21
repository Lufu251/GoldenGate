"""Turn template files into a desired-state document.

Everything left of the check belongs here: reading a template and its
variables, rendering one against the other, refusing a document no
firewall could satisfy, and folding several documents into one.

::

    templates/<name>.yaml + vars/<host>.yaml
         --render--> data/rendered/<host>/<name>.yaml
         --merge---> data/desired/<host>.yaml

Both artifacts have the same schema as a file in ``templates/``: cmdb
paths at the top level, variables resolved, Jinja2 gone. There is no
wrapper and no ``host:`` key, so one reader/writer pair serves both and
:func:`~fortigate.compliance.checker.check_template` is handed exactly
what it was always handed.

Neither file carries its host. ``data/normal/<host>.yaml`` does, because
it also carries facts discovered from the appliance -- serial, version,
build -- that have nowhere else to live; a rendered template has none.
The accepted cost is that a desired file copied to another host's name is
checked without complaint, against a ``data/`` that is generated and
rebuilt by one command anyway.

A fault in the template raises :class:`TemplateError` here, before
anything is compared against a firewall. Whether the rules are
well-formed is a question about the rules alone, and answering it early
means an unsatisfiable assertion never reaches a diff that claims to
describe the firewall.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Tuple, Union

import yaml
from jinja2 import Environment, StrictUndefined

# Jinja2's own base exception shares our name. Aliased rather than
# referenced as ``jinja2.TemplateError``, so the unqualified name in this
# module always means ours.
from jinja2 import TemplateError as JinjaError

__all__ = [
    "TemplateError",
    "load_template",
    "load_vars",
    "render",
    "validate",
    "coerce",
    "merge",
    "rendered_file_path",
    "desired_file_path",
    "write_document",
    "load_document",
]


class TemplateError(Exception):
    """The template asserts something no firewall state could satisfy.

    Raised rather than reported: the fault is in a git-tracked, reviewed
    file, and one bad line aborting that host's run is better than a
    finding that reads as a firewall problem.
    """


def load_template(path: Path) -> str:
    """Read one template file as text.

    Split out from :func:`render` so rendering is a function of two
    in-memory values. Fusing the read to it would force a fixture file
    into every test of the render itself.
    """
    return Path(path).read_text()


def load_vars(path: Path) -> Dict[str, Any]:
    """Load one firewall's template variables.

    One vars file feeds every template for a host: a variable is a fact
    about the firewall, not about the template consuming it.
    """
    return yaml.safe_load(Path(path).read_text()) or {}


def render(text: str, variables: Dict[str, Any]) -> Any:
    """Render template ``text`` with ``variables`` and parse the result.

    The text is rendered and *then* parsed, rather than the file being
    parsed and each string leaf rendered. That keeps ``{% for %}`` and
    ``{% if %}`` available -- generating one address object per VLAN is
    exactly what a parameterised template is for -- at the cost of making
    quoting the author's job. An unquoted placeholder inherits YAML type
    inference, so a variable holding ``on`` or ``no`` becomes a bool;
    :func:`validate` rejects those rather than letting ``'True'`` be
    compared against a FortiOS ``enable``.

    ``StrictUndefined`` is not optional. Jinja2's default renders an
    undefined variable as the empty string, which would silently assert
    the wrong expected value and report a FAIL against a value nobody
    wrote.

    Both Jinja2 failures and YAML parse failures come back out as
    :class:`TemplateError`: both are faults in the rules, and a caller
    should need one except clause rather than one per library that
    happened to notice.

    Whatever the YAML parsed to is returned without checking its shape.
    That is :func:`validate`'s job, one call later. A file that renders
    to nothing is a document asserting nothing, not a null document.
    """
    environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    try:
        rendered = environment.from_string(text).render(**variables)
    except JinjaError as error:
        raise TemplateError(str(error)) from error
    try:
        return yaml.safe_load(rendered) or {}
    except yaml.YAMLError as error:
        raise TemplateError(f"rendered to invalid YAML: {error}") from error


def _coordinate(path: Tuple[str, ...]) -> str:
    """Describe a key path from the document root for an error message.

    :func:`~fortigate.compliance.checker._where`'s format without the
    vdom, since a template is not scoped to one.
    """
    return " / ".join(path)


def validate(document: Any, where: Tuple[str, ...] = ()) -> None:
    """Raise :class:`TemplateError` on anything no firewall could satisfy.

    Three faults, checked over the whole document rather than wherever a
    comparison happens to reach:

    - **Not a mapping.** A template rendering to a list or a bare string
      is not a template; ``check_template`` would fail on ``.items()``.
    - **A bool, at any depth.** FortiOS has no booleans anywhere -- it
      uses ``enable``/``disable`` strings -- so a bool can only be an
      unquoted placeholder that hit YAML type inference.
    - **A null, at any depth.** ``admin-concurrent:`` with nothing after
      it parses to ``None``, and left alone it becomes a FAIL of
      ``expected: None`` against every firewall forever: a finding that
      reads as a firewall problem and cannot be fixed on the firewall.

    Validating up front rather than during the walk is the point. A bool
    under a path that was never exported is never looked at by the
    checker, so fixing the *export* months later would surface a template
    bug that was always there as a regression in the export.

    The walk descends into lists, which is where the checker cannot see:
    :func:`coerce` reduces a list to a frozenset of strings, so
    ``srcintf: [yes]`` would compare a stringified ``True`` rather than
    raise. List items are named by their containing key, as ``coerce``
    names them -- the offending value is in the message either way.

    Coordinates are named, never files: the file name is added by
    whichever script catches, which is the only thing that knows it.
    """
    if isinstance(document, Mapping):
        for key, value in document.items():
            validate(value, where + (str(key),))
        return

    # Only the root has to be a mapping; below it, anything that is not
    # one is a leaf to be checked.
    if not where:
        raise TemplateError(
            f"the document is a {type(document).__name__}, not a mapping of "
            f"cmdb paths"
        )

    if isinstance(document, list):
        for item in document:
            validate(item, where)
        return

    if isinstance(document, bool):
        raise TemplateError(
            f"{_coordinate(where)}: {document!r} is a bool, which FortiOS "
            f"never returns -- quote the value so it stays a string"
        )
    if document is None:
        raise TemplateError(
            f"{_coordinate(where)}: the key has no value, and null matches "
            f"nothing a firewall can hold -- give it a value or drop the key"
        )


def coerce(value: Any, where: str) -> Union[str, FrozenSet[str]]:
    """Reduce a value to what "the same value" means here, or reject it.

    Scalars compare as strings because the API is inconsistent about
    types within a single file: ``admintimeout`` comes back as an int
    while ``purdue-level: '3'`` and ``session-ttl: '0'`` are strings.
    Coercing *numerically* instead would collapse
    ``diffservcode-forward: '000000'`` to ``0`` and silently match a
    template written as ``0``. ``str`` refuses that case loudly and the
    template author quotes the value.

    Lists compare as sets. After normalization every list is either empty
    or a collapsed reference list (``srcintf: ['wan1']``,
    ``service: ['HTTPS']``), and none of them are order-sensitive --
    policy *evaluation* order is the order of the ``firewall/policy``
    mapping itself, not of any field inside a policy.

    A mapping raises. This is the one rejection that has to live here:
    only this function descends into lists, so only it can see a mapping
    nested in one. Unlike the two mapping-vs-scalar cases in the checker,
    it does not know which side it is holding, so it names the coordinate
    and says the value has nothing comparable rather than claiming the
    template and the firewall disagree.

    Bools and nulls are *not* rejected. :func:`validate` catches those
    across a whole document, and the firewall cannot produce them: the
    raw API's only booleans are envelope fields such as
    ``limit_reached``, ``normalize_section`` descends into ``results``
    alone, and a real normalized host measured 5939 scalar values -- 5531
    strings and 408 ints -- without a single bool or null among them. A
    branch guarding an unreachable case is worth less than one fewer
    branch.
    """
    if isinstance(value, Mapping):
        raise TemplateError(
            f"{where}: a mapping has no comparable value; something here is "
            f"an object where the other side holds a field"
        )
    if isinstance(value, list):
        return frozenset(coerce(item, where) for item in value)
    return str(value)


def merge(
    base: Mapping, overlay: Mapping, where: Tuple[str, ...] = ()
) -> Dict[str, Any]:
    """Fold ``overlay`` into ``base``, refusing any disagreement.

    Binary rather than variadic: iterating over many documents is the
    caller's job, and the caller accumulates from ``{}``, which is the
    identity for a deep merge. So a host with a single template needs no
    special case -- its document is written through unchanged.

    The merge is **deep**. Two templates touching different fields of one
    ``firewall/address`` object stack rather than clobbering at the cmdb
    path, at every level: a role adding ``ntpv3`` to ``system/ntp``'s
    ``ntpserver`` row 1 leaves the baseline's ``server`` on that row
    intact.

    Identical values are not a conflict, judged through :func:`coerce`,
    so a value written as a placeholder in one file and a literal in the
    other still matches, and two templates asserting one list in
    different orders agree for the same reason the checker considers them
    equal. Anything else raises:

    - **Differing values at one coordinate.** Two templates disagreeing
      is a fault in the rules, not something to resolve by picking one.
    - **A mapping against a scalar.** One template treating a coordinate
      as an object and the other as a field is a disagreement about
      shape, and letting either win would drop the other's assertions.

    Keys match by ``str``, for the reason ``_by_str_key`` exists in the
    checker -- ``firewall/policy`` is keyed by int and
    ``firewall/address`` by string -- and the base's key object is the
    one written, so an overlay cannot change how a coordinate is spelled.
    The checker re-keys by ``str`` before every lookup, so nothing
    downstream can tell.

    Insertion order is the base's, then the overlay's new keys.

    ``where`` is the coordinate this call sits at, carried by the
    recursion so a conflict deep in a document can name its full key
    path. Callers merging two documents pass nothing.
    """
    merged = dict(base)
    base_keys = {str(key): key for key in base}

    for raw_key, overlay_value in overlay.items():
        key = str(raw_key)
        here = where + (key,)

        if key not in base_keys:
            merged[raw_key] = overlay_value
            continue

        base_key = base_keys[key]
        base_value = merged[base_key]

        if isinstance(base_value, Mapping) and isinstance(overlay_value, Mapping):
            merged[base_key] = merge(base_value, overlay_value, here)
            continue

        coordinate = _coordinate(here)
        if isinstance(base_value, Mapping) or isinstance(overlay_value, Mapping):
            raise TemplateError(
                f"{coordinate}: one template asserts an object here and "
                f"another a single value; neither can be dropped"
            )

        if coerce(base_value, coordinate) != coerce(overlay_value, coordinate):
            raise TemplateError(
                f"{coordinate}: two templates assert different values here "
                f"-- {base_value!r} and {overlay_value!r}"
            )

    return merged


def rendered_file_path(output_dir: Path, host_name: str, template_name: str) -> Path:
    """Compute where one rendered template should be written."""
    return Path(output_dir) / host_name / f"{template_name}.yaml"


def desired_file_path(output_dir: Path, host_name: str) -> Path:
    """Compute where a host's merged desired state should be written."""
    return Path(output_dir) / f"{host_name}.yaml"


def write_document(file_path: Path, document: Mapping) -> None:
    """Validate ``document`` and write it to ``file_path`` as YAML.

    Validating here rather than in the scripts means an invalid document
    can neither be written nor read back: the guard is the writer, not an
    instruction each caller has to remember. Nothing is written when it
    raises, so a template fault never leaves a half-valid artifact on
    disk to be checked against later.

    ``sort_keys=False`` for the reason ``write_normalized`` uses it: key
    order carries meaning in this data, and the merge already decided it.
    """
    validate(document)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False)
    )


def load_document(file_path: Path) -> Dict[str, Any]:
    """Load a rendered or merged document, validating what was read.

    Raises :class:`FileNotFoundError` if the file is absent, for the
    reason :func:`~fortigate.compliance.checker.load_normalized` does: an
    empty document yields zero findings and a clean-looking diff for a
    firewall nobody ever wrote a policy for.

    The validate is the only guard against a stale or hand-edited file.
    These documents have no wrapper and therefore no decoder to act as
    the chokepoint, so the loader carries it.
    """
    document = yaml.safe_load(Path(file_path).read_text())
    validate(document)
    return document
