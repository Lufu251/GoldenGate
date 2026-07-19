"""Check a normalized firewall config against a desired-state template.

The checker never touches an appliance. It reads
``data/normal/<host>.yaml`` -- the output of
:mod:`fortigate.config.normalizer` -- and compares it against an
already-rendered template, producing a :class:`ComplianceResult` that
:func:`write_diff` renders to ``data/diff/<host>.yaml``.

Matching is **subset**: everything the template declares must be present
and correct, while objects and fields the firewall has but the template
does not are ignored. A firewall carries hundreds of settings nobody ever
chose, so asserting on absence would mean transcribing the whole box.

Three things can be said about a template entry:

FAIL (:class:`Violation`)
    the value was checked and is wrong.
MISSING (:class:`MissingKey`)
    the template expects a key the firewall does not have, at any depth.
UNKNOWN (:class:`UnknownPath`)
    the path was never exported, so nothing was checked. Kept apart from
    FAIL so a broken *export* does not read as a broken *firewall*.

A template bug is none of those. An undefined Jinja2 variable, a bool
where FortiOS has only ``enable``/``disable`` strings, and a scalar
asserted against a mapping are all statements about the template, not
about the firewall, so they raise rather than being written into a diff
that claims to describe the firewall's compliance.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple, Union

import yaml
from jinja2 import Environment, StrictUndefined

from ..config.normalizer import NormalizedHost

__all__ = [
    "GLOBAL_VDOM",
    "TemplateError",
    "Violation",
    "MissingKey",
    "UnknownPath",
    "ComplianceResult",
    "load_vars",
    "render_template",
    "load_normalized",
    "comparable",
    "check_object",
    "check_section",
    "check_template",
    "diff_file_path",
    "write_diff",
]

#: The ``vdom`` recorded on findings that came from whole-appliance config.
#: A reporting label, not a VDOM that exists on the box -- and deliberately
#: not :data:`fortigate.config.sections.GLOBAL_SCOPE`, which names a legal
#: scope declaration in ``sections.yaml``. The two are equal by
#: coincidence.
GLOBAL_VDOM = "global"

#: Marks a key that is absent from the firewall, distinguishing it from a
#: key genuinely set to ``None``.
_ABSENT = object()


class TemplateError(Exception):
    """The template asserts something no firewall state could satisfy.

    Raised rather than reported: the fault is in a git-tracked, reviewed
    file, and one bad line aborting that host's check is better than a
    finding that reads as a firewall problem.
    """


@dataclass(frozen=True)
class Violation:
    """A value that was checked and is wrong."""

    vdom: str
    path: str
    #: ``None`` when the field belongs to a singleton section itself, as
    #: ``system/global``'s do, rather than to an object within a table.
    object_key: Optional[str]
    #: Path segments below the object. A tuple, not a dotted string:
    #: object and field names contain dots (``fqdn_api.cloudflare.com``),
    #: so splitting one back apart would invent segments.
    field: Tuple[str, ...]
    expected: str
    actual: str


@dataclass(frozen=True)
class MissingKey:
    """A key the template expects that the firewall does not have."""

    vdom: str
    path: str
    object_key: Optional[str]
    #: Empty when the whole object is missing.
    field: Tuple[str, ...]
    #: A scalar, or the mapping that was expected.
    expected: Any


@dataclass(frozen=True)
class UnknownPath:
    """A template path that was never exported for this VDOM.

    Carries no object key or field: nothing below the path was examined,
    so there are no coordinates to report.
    """

    vdom: str
    path: str


#: What a comparison at one key can produce. A path is ruled unknown one
#: level up, before there is anything to compare, so it never appears here.
Finding = Union[Violation, MissingKey]


@dataclass
class ComplianceResult:
    """Every finding for one firewall.

    Three flat lists rather than a ``Dict[vdom][path]`` tree: the findings
    already carry their own coordinates, and a tree would encode them a
    second time and let the two disagree. Grouping happens once, on the
    way out, in :meth:`to_diff_mapping`.
    """

    host: str
    violations: List[Violation] = dataclass_field(default_factory=list)
    missing: List[MissingKey] = dataclass_field(default_factory=list)
    unknown: List[UnknownPath] = dataclass_field(default_factory=list)

    @property
    def is_compliant(self) -> bool:
        """True if the firewall matched the template.

        Ignores :attr:`unknown` -- an unexported path is a gap in stage 1,
        which fails its own run loudly, not a fault in the firewall.
        """
        return not self.violations and not self.missing

    def add(self, findings: List[Finding]) -> None:
        """File each finding under its own list."""
        for finding in findings:
            if isinstance(finding, Violation):
                self.violations.append(finding)
            else:
                self.missing.append(finding)

    def to_diff_mapping(self) -> Dict[str, Any]:
        """Render to the plain dict that gets written as YAML.

        The document follows the *normalized* shape -- ``global`` as a
        sibling of ``vdoms``, then cmdb path, object key, field -- so a
        finding sits at the exact path you would navigate to in
        ``data/normal/<host>.yaml``. Only the leaf differs: an
        ``expected``/``actual`` pair instead of a value.

        MISSING and UNKNOWN encode structurally. For a missing value the
        ``actual`` key is simply absent, because there was no value to
        write; ``actual: missing`` would be indistinguishable from a
        firewall genuinely holding the string ``missing``.
        """
        mapping: Dict[str, Any] = {"host": self.host}

        for violation in self.violations:
            node = self._node_for(mapping, violation)
            node[violation.field[-1]] = {
                "expected": violation.expected,
                "actual": violation.actual,
            }

        for missing in self.missing:
            node = self._node_for(mapping, missing)
            if missing.field:
                node[missing.field[-1]] = {"expected": missing.expected}
            else:
                node["_status"] = "missing"

        for unknown in self.unknown:
            self._scope_of(mapping, unknown.vdom).setdefault(unknown.path, {})[
                "_status"
            ] = "unknown"

        return mapping

    @staticmethod
    def _scope_of(mapping: Dict[str, Any], vdom: str) -> Dict[str, Any]:
        """Return the ``global`` or ``vdoms/<vdom>`` container for ``vdom``."""
        if vdom == GLOBAL_VDOM:
            return mapping.setdefault("global", {})
        return mapping.setdefault("vdoms", {}).setdefault(vdom, {})

    @classmethod
    def _node_for(
        cls, mapping: Dict[str, Any], finding: Union[Violation, MissingKey]
    ) -> Dict[str, Any]:
        """Descend to the dict a finding's leaf is written into.

        ``object_key`` being optional is what lets ``system/global``'s own
        fields and ``firewall/address``'s objects share one routine: when
        it is ``None``, that coordinate is skipped.
        """
        node = cls._scope_of(mapping, finding.vdom).setdefault(finding.path, {})
        if finding.object_key is not None:
            node = node.setdefault(finding.object_key, {})
        for segment in finding.field[:-1]:
            node = node.setdefault(segment, {})
        return node


def load_vars(path: Path) -> Dict[str, Any]:
    """Load one firewall's template variables."""
    return yaml.safe_load(Path(path).read_text()) or {}


def render_template(path: Path, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Render a template file with ``variables`` and parse the result.

    The *text* is rendered and then parsed, rather than the file being
    parsed and each string leaf rendered. That keeps ``{% for %}`` and
    ``{% if %}`` available -- generating one address object per VLAN is
    exactly what a parameterised template is for -- at the cost of making
    quoting the author's job. An unquoted placeholder inherits YAML type
    inference, so a variable holding ``on`` or ``no`` becomes a bool;
    :func:`comparable` rejects those rather than letting ``'True'`` be
    compared against a FortiOS ``enable``.

    ``StrictUndefined`` is not optional. Jinja2's default renders an
    undefined variable as the empty string, which would silently assert
    the wrong expected value and report a FAIL against a value nobody
    wrote.
    """
    path = Path(path)
    environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    rendered = environment.from_string(path.read_text()).render(**variables)
    return yaml.safe_load(rendered) or {}


def load_normalized(normal_dir: Path, host_name: str) -> NormalizedHost:
    """Load one firewall's normalized config.

    Raises :class:`FileNotFoundError` if the host was never normalized.
    Unlike :func:`~fortigate.config.normalizer.normalize_host`, which
    reads a directory tree where partial presence is normal and
    meaningful, this reads one file: it exists or it does not. Returning
    an empty host would make the cross product empty, yield zero
    findings, and write a clean-looking diff for a firewall nobody ever
    checked.
    """
    file_path = Path(normal_dir) / f"{host_name}.yaml"
    return NormalizedHost.from_mapping(yaml.safe_load(file_path.read_text()))


def _where(vdom: str, path: str, object_key: Optional[str], field: Tuple[str, ...]) -> str:
    """Describe a coordinate for a :class:`TemplateError` message."""
    parts = [vdom, path]
    if object_key is not None:
        parts.append(object_key)
    parts.extend(field)
    return " / ".join(parts)


def comparable(value: Any, where: str) -> Union[str, FrozenSet[str]]:
    """Coerce one side of a scalar comparison, or reject it.

    Scalars compare as strings on both sides because the API is
    inconsistent about types within a single file: ``admintimeout`` comes
    back as an int while ``purdue-level: '3'`` and ``session-ttl: '0'``
    are strings. Coercing *numerically* instead would collapse
    ``diffservcode-forward: '000000'`` to ``0`` and silently match a
    template written as ``0``. ``str`` refuses that case loudly and the
    template author quotes the value.

    Lists compare as sets. After normalization every list is either empty
    or a collapsed reference list (``srcintf: ['wan1']``,
    ``service: ['HTTPS']``), and none of them are order-sensitive --
    policy *evaluation* order is the order of the ``firewall/policy``
    mapping itself, not of any field inside a policy.

    Two values raise instead. A bool cannot have come from FortiOS, which
    has no booleans anywhere -- it uses ``enable``/``disable`` strings --
    so it can only be an unquoted placeholder that hit YAML type
    inference. A mapping reaching a scalar comparison is a kind mismatch,
    structurally impossible for any firewall state to satisfy. Both are
    template bugs, and ``str``-ing them would produce a stringified dict
    that reads as a firewall problem.
    """
    if isinstance(value, bool):
        raise TemplateError(
            f"{where}: {value!r} is a bool, which FortiOS never returns -- "
            f"quote the value so it stays a string"
        )
    if isinstance(value, Mapping):
        raise TemplateError(
            f"{where}: a mapping cannot be compared against a scalar; "
            f"the template and the firewall disagree about the shape here"
        )
    if isinstance(value, list):
        return frozenset(comparable(item, where) for item in value)
    return str(value)


def _describe(value: Any) -> str:
    """Format a value for the ``expected``/``actual`` leaf of a diff."""
    if isinstance(value, list):
        return ", ".join(sorted(str(item) for item in value))
    return str(value)


def _by_str_key(mapping: Mapping) -> Dict[str, Any]:
    """Re-key a mapping by ``str``, so lookups do not depend on YAML types.

    ``firewall/policy`` is keyed by int (``4``, ``29``) and
    ``firewall/address`` by string, in the same file. Without this a
    template written ``"1":`` instead of ``1:`` would report a spurious
    missing object with no hint why.
    """
    return {str(key): value for key, value in mapping.items()}


def check_object(
    expected: Mapping,
    actual: Mapping,
    vdom: str,
    path: str,
    object_key: Optional[str],
    field: Tuple[str, ...] = (),
) -> List[Finding]:
    """Compare one expected mapping against the firewall's, recursively.

    Recursion is not a special case for nested tables: ``normalize_value``
    gave every level the same shape, so ``system/ntp``'s ``ntpserver``
    rows are keyed by ``id`` exactly as ``firewall/policy`` is keyed by
    mkey, and the same code applies one layer down.

    Whole-dict ``==`` was rejected because it breaks the subset rule
    precisely where that is least obvious -- asserting one field of an
    ``ntpserver`` row would mean reproducing all eight -- and turns the
    diff leaf into two blobs to eyeball-diff.
    """
    findings: List[Finding] = []
    actual_by_key = _by_str_key(actual)

    for raw_key, expected_value in expected.items():
        key = str(raw_key)
        here = field + (key,)
        actual_value = actual_by_key.get(key, _ABSENT)

        if actual_value is _ABSENT:
            findings.append(MissingKey(vdom, path, object_key, here, expected_value))
            continue

        if isinstance(expected_value, Mapping):
            if not isinstance(actual_value, Mapping):
                raise TemplateError(
                    f"{_where(vdom, path, object_key, here)}: the template "
                    f"expects a mapping but the firewall holds a scalar"
                )
            findings.extend(
                check_object(
                    expected_value, actual_value, vdom, path, object_key, here
                )
            )
            continue

        where = _where(vdom, path, object_key, here)
        if comparable(expected_value, where) != comparable(actual_value, where):
            findings.append(
                Violation(
                    vdom,
                    path,
                    object_key,
                    here,
                    _describe(expected_value),
                    _describe(actual_value),
                )
            )

    return findings


def check_section(
    path: str, expected: Mapping, actual: Mapping, vdom: str
) -> List[Finding]:
    """Check one cmdb path's expectations against the firewall's config.

    There is deliberately **no table-vs-singleton dispatch**. After
    normalization everything is a dict, and the distinction is not
    reliably in the data: ``system/ntp`` is a singleton that *contains* a
    mapping (``ntpserver``), and an empty ``firewall/policy`` carries no
    evidence either way -- so sniffing value types guesses wrong on both,
    on real data in this repo.

    The one thing decided here is the *coordinate* a depth-one key gets.
    A mapping-valued key names an object (``firewall/address``'s
    ``hq-lan``); a scalar-valued one is a field of the section itself
    (``system/global``'s ``admintimeout``). That only chooses which slot
    of the finding the key lands in -- both render to the same place --
    so a wrong guess costs nothing structural.
    """
    findings: List[Finding] = []
    actual_by_key = _by_str_key(actual)

    for raw_key, expected_value in expected.items():
        key = str(raw_key)
        if not isinstance(expected_value, Mapping):
            findings.extend(
                check_object({key: expected_value}, actual, vdom, path, None)
            )
            continue

        actual_value = actual_by_key.get(key, _ABSENT)
        if actual_value is _ABSENT:
            # The whole object is absent. An exported-but-empty section is
            # the firewall's real state, so its objects are MISSING and the
            # path is emphatically not UNKNOWN.
            findings.append(MissingKey(vdom, path, key, (), expected_value))
            continue
        if not isinstance(actual_value, Mapping):
            raise TemplateError(
                f"{_where(vdom, path, key, ())}: the template expects a "
                f"mapping but the firewall holds a scalar"
            )
        findings.extend(check_object(expected_value, actual_value, vdom, path, key))

    return findings


def check_template(
    template: Mapping, normal_dir: Path, host_name: str
) -> ComplianceResult:
    """Check one rendered template against one normalized firewall.

    A vdom-scoped path is checked in **every** VDOM: that cross product is
    the semantics, the same shape as ``build_export_plan`` in the
    exporter, and nothing in the template declares the loop.

    A global path is checked **once**, against ``global``, or one global
    misconfiguration is reported N times and ``system/global`` looks
    missing from every VDOM. Which paths are global is derived from the
    normalized file itself -- if a path is present under ``global``, it is
    global -- rather than re-read from ``configuration/sections.yaml``,
    the same way ``discover_scope_dirs`` reads what is on disk instead of
    trusting a declared list. That needs no second input and cannot drift
    out of step with the file being checked.
    """
    host = load_normalized(normal_dir, host_name)
    result = ComplianceResult(host=host.host)

    for path, expected in template.items():
        if path in host.global_config:
            result.add(
                check_section(path, expected, host.global_config[path], GLOBAL_VDOM)
            )
            continue

        for vdom, sections in host.vdoms.items():
            if path not in sections:
                # Reported per VDOM rather than hoisted: the exporter
                # records fetch failures per (vdom, path), so a section can
                # genuinely be present in one VDOM and absent in another,
                # and that asymmetry is the case most worth seeing. It also
                # follows from the scope rule -- a path present nowhere
                # gives the checker no way to know whether it was meant to
                # be global, so it can only say where it looked.
                result.unknown.append(UnknownPath(vdom, path))
            else:
                result.add(check_section(path, expected, sections[path], vdom))

    return result


def diff_file_path(output_dir: Path, host_name: str) -> Path:
    """Compute where a host's diff should be written."""
    return Path(output_dir) / f"{host_name}.yaml"


def write_diff(file_path: Path, result: ComplianceResult) -> None:
    """Write ``result`` to ``file_path`` as YAML.

    A clean run still writes the file, carrying ``host`` and nothing
    else. "Checked, compliant" and "never ran" must not look alike.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        yaml.safe_dump(
            result.to_diff_mapping(), sort_keys=False, default_flow_style=False
        )
    )
