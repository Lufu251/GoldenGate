# Compliance checker

Goal: define parameterised templates of expected firewall configuration, and
check exported config against them to see whether a firewall is compliant.

The pipeline is three stages, each writing to disk so the next can be
developed and tested on its own:

```
appliance --export--> data/raw/<host>/<scope>/cmdb-*.json
          --normalize--> data/normal/<host>.yaml
          --check--> data/diff/<host>.yaml
```

`<scope>` is either a VDOM name or `global`. In multi-VDOM mode some
config (`system/global`, `system/ntp`) exists once for the whole
appliance rather than per-VDOM, so it is exported once and normalized
into a top-level `global:` key that is a *sibling* of `vdoms:`, not an
entry inside it.

All three stages are implemented. The stage 3 sections below are kept as
written: they are the reasoning behind `fortigate/compliance/checker.py`,
not a to-do list.

The package is split by responsibility: `fortigate/api/` talks to the
appliance, `fortigate/config/` fetches and reshapes what it returns.
`config` depends on `api`; the reverse never happens. Stage 3 adds a
third package alongside them.

---

## Stage 1: config exporter -- IMPLEMENTED

Reads config off the appliance over the REST API and writes each section
to disk unchanged.

- `fortigate/api/client.py` -- HTTP/auth against the API.
- `fortigate/api/inventory.py` -- which firewalls exist (`inventory.yaml`).
- `fortigate/config/sections.py` -- which sections to fetch and the scope each
  lives in (`configuration/sections.yaml`).
- `fortigate/config/exporter.py` -- discovers the VDOMs, fetches each
  section in its scope, writes the response envelope as JSON.
- `scripts/export_fw1.py` -- runnable.

Output: `data/raw/<host>/<scope>/cmdb-*.json`, the full API envelope
verbatim. A section that fails to fetch is recorded and skipped rather
than aborting the export.

---

## Stage 2: normalizer -- IMPLEMENTED

Rewrites the raw JSON into one diff-friendly YAML file per firewall.

- `fortigate/config/normalizer.py` -- the transform.
- `scripts/normalize_fw1.py` -- runnable.

Output: `data/normal/<host>.yaml`.

```yaml
host: fw1
version: v7.4.12
global:
  system/ntp:
    ntpsync: enable
vdoms:
  root:
    firewall/policy:
      4:
        name: wan-dmz-rproxy
        srcintf: [wan1]
```

Envelope metadata and per-object bookkeeping (`q_origin_key`, `uuid`) are
dropped, reference lists collapse to plain names, and tables become
mappings keyed by their mkey -- inferred from the data, so a diff stays
local to the object that actually changed. An empty table normalizes to
`{}`, not `[]`, so a section's type does not depend on whether the
firewall happens to have rows in it; empty *field* lists (`macaddr: []`)
stay lists, and only `normalize_section` can tell the two apart. Appliance facts lift to the
top, the `cmdb/` prefix drops, and API order is preserved (load-bearing
for `firewall/policy`, where it is evaluation order).

---

## Future work

### Default suppression -- the big one

`system/global` still normalizes to **238 keys**, of which maybe ten were
ever set by an operator. The structure problem is fixed; the volume problem
is not, and it is where the real readability win lives. Three ways to do it:

- **Subtract a factory-default export** -- accurate, needs no schema, but
  needs one baseline export per FortiOS version and a spare appliance.
- **Subtract the API schema** (`?action=schema` returns each attribute's
  `default`) -- self-describing and per-version-correct, costs one extra API
  call per path at export time. Preferred.
- **Keep everything** -- current behaviour.

Worth doing as a separate pass over `data/normal/<host>.yaml` rather than
folding it into the normalizer, so it can be developed and tested
independently. Note it also largely answers the open template-versioning
question -- see "Out of scope: which template gets checked against which
firewall" below.

### Generating standard FortiGate config

Two distinct outputs, both rendering from `data/normal/<host>.yaml`:

- **CLI text** (`config firewall address` / `edit "x"` / `set ...` / `next` /
  `end`). The mkey-map shape maps 1:1 onto `edit <mkey>` blocks.
- **API payloads** for pushing config back.

Key insight for whoever builds this: FortiGate CLI is itself
default-suppressed (`show` omits defaults, `show full-configuration` does
not). So once default suppression lands, the normalized YAML *is* `show`, and
CLI generation becomes correct by construction. One transform serves both
diffing and generation -- another reason to do defaults before generation.

Open question for that stage: is generated config meant to be
round-trippable (export -> YAML -> CLI -> apply, same box back), or just
human-readable for review? Round-trip raises the stakes on ordering.

---

## Stage 3: compliance checker -- IMPLEMENTED

One thing the plan did not anticipate, learned on first run: because the
template is rendered as *text*, the whole file is template source --
**YAML comments included**. A literal Jinja2 tag written in a comment is
still parsed as a tag, and a lone `{% for %}` in prose is a syntax error
that fails the render. Documented in `templates/baseline.yaml` itself.

### New files

- `templates/baseline.yaml` -- desired-state template, keyed by cmdb path.
  Jinja2 placeholders for anything parameterised.
- `vars/fw1.yaml` -- one file per firewall, flat mapping of variable name ->
  value. Tracked in git (no secrets -- those stay in the gitignored
  `inventory.yaml`).
- `fortigate/compliance/checker.py` -- the checking logic, in its own
  package alongside `api/` and `config/`. It never touches the appliance:
  it reads the normalized YAML off disk, via `NormalizedHost.from_mapping`
  (see below), so it works on a typed object and never indexes the
  document by key name. So `compliance -> config -> api`, one direction,
  no cycle.
- `scripts/check_fw1.py` -- runnable, mirroring `scripts/export_fw1.py`.

One addition to stage 2: `NormalizedHost.from_mapping()`, the inverse of
`to_mapping`. `to_mapping` names the document's keys (`global`, `vdoms`)
as literals; without an inverse next to it, stage 3 would re-derive that
schema by hand and hold a second copy of those names. Note `GLOBAL_SCOPE`
is *not* that name: it is one of the two legal scope declarations in
`sections.yaml` (`SCOPES`), used by the normalizer to match a raw-export
*directory* name. The two are equal only by coincidence, and `vdoms` has
no constant at all. Encoder and decoder live together, and the checker
touches `.global_config` / `.vdoms` instead of either.

Output: `data/diff/<host>.yaml`. Like every other stage, stage 3 writes
its result to disk rather than only printing it -- see "Diff output"
below.

### Consequences for stage 3 of the normalizer landing

The template shape gets simpler, because two of stage 3's original open
questions are now answered by the layer beneath it:

- **`key:` is gone.** Templates no longer declare the mkey per path; the
  normalizer already keyed every table. Expected objects are written as a
  mapping keyed the same way, and matching is a dict lookup rather than a
  scan.
- **Nested comparison got uniform, not absent.** The original worry was
  comparing `srcintf: [{"name": "port1"}]`; normalized, that's
  `srcintf: ["port1"]` and a plain `==` works. Nested *tables* remain
  (`system/ntp` holds `ntpserver`), but the normalizer gave them the same
  mkey-keyed shape as top-level ones, so one recursive comparison handles
  every depth instead of a per-level special case. Type coercion (the API
  returning numbers as strings) still needs handling.
- **`load_normalized` targets `data/normal/<host>.yaml`**, not `data/raw`.
  It reads one file per host rather than reassembling many.

### Template shape

```yaml
system/global:
  hostname: "{{ hostname }}"
  admintimeout: 15

firewall/address:
  mgmt-subnet:
    subnet: "{{ mgmt_subnet }}"
  "{{ site }}-lan":
    subnet: "{{ lan_subnet }}"

firewall/policy:
  1:
    action: accept
    logtraffic: all
```

Every value may be a literal or a `{{ var }}` placeholder.

### `vars/fw1.yaml`

```yaml
hostname: fw1
site: hq
mgmt_subnet: 10.0.0.0/24
lan_subnet: 192.168.1.0/24
```

### `fortigate/compliance/checker.py`

Dataclasses plus small pure functions, same style as `config/exporter.py`
and `config/normalizer.py`.

One firewall per call, like `export_sections` and `normalize_host`. The
already-rendered template is passed *in*; the checker never reads a file
and never imports Jinja2. Looping over a fleet lives in the script, and
combining several templates into one becomes a separate module producing a
template dict, with no change here. That module is now known to be needed
(mixed models, branch vs DC) but is deliberately not built in this stage.

- `load_vars(path) -> dict`
- `render_template(path, variables) -> dict` -- Jinja2-render the text, then
  `yaml.safe_load`. Uses `StrictUndefined`: an undefined `{{ var }}`
  renders as an empty string by default, which silently asserts the wrong
  expected value. Failing at render time is the only safe behaviour.

  Rendering the *text* and then parsing -- rather than parsing first and
  rendering each string leaf -- is deliberate: it keeps `{% for %}` /
  `{% if %}` available, and generating one address object per VLAN is
  exactly what the "same template can later push config to a new
  firewall" decision is for. Parsing first would foreclose that and is
  hard to undo. The cost is that quoting is the template author's job,
  and an *unquoted* placeholder inherits YAML type inference: a var value
  of `on` or `no` becomes a bool, which stringifies to `'True'` and can
  never match a FortiOS `enable`/`disable` -- a FAIL against a value the
  firewall could not possibly hold. Hence the bool guard below.
- `load_normalized(normal_dir, host_name) -> NormalizedHost` -- raises
  `FileNotFoundError` if the host was never normalized. Unlike
  `normalize_host`, which reads a directory tree where partial presence is
  normal and meaningful, this reads one file: it exists or it does not.
  Returning an empty `NormalizedHost` would make the cross product empty,
  yield zero findings, and write a clean-looking diff for a host that was
  never checked -- exactly the "checked, compliant" vs "never ran"
  confusion this stage is built to avoid. Mostly this is *not* catching
  the exception `read_text` already raises.
- `check_object(expected, actual, ...) -> List[Violation | MissingKey]` --
  recursive subset comparison, using the semantics below. Never returns
  `UnknownPath`: that is decided one level up, by `check_template`, before
  there is anything to compare.
- `check_section(path, expected, actual, vdom) -> List[Violation | MissingKey]`
  -- `check_object` applied at section level. There is deliberately **no
  table-vs-singleton dispatch**: after normalization everything is a dict,
  and the distinction is not reliably in the data. `system/ntp` is a
  singleton object that *contains* a mapping (`ntpserver`), and `blulab`'s
  empty `firewall/policy` carries no evidence either way -- so sniffing
  value types guesses wrong on both, on real data in this repo. Recursion
  is correct at every depth for the same reason it is correct at depth one
  (see "Nested values"), and that argument does not stop at the section
  boundary. Objects and fields present on the firewall but absent from the
  template are ignored.
- `check_template(template, normal_dir, host_name) -> ComplianceResult` --
  every VDOM x every path in the template. Carries `host`, so a script
  looping over several firewalls can print and aggregate without threading
  identity alongside the result.
- `ComplianceResult.to_diff_mapping() -> dict` -- render the result tree
  to the plain dict that gets written as YAML, exactly as
  `NormalizedHost.to_mapping` does.
- `diff_file_path(output_dir, host_name) -> Path` and
  `write_diff(file_path, result) -> None` -- mirroring `host_file_path`
  and `write_normalized`. The output directory comes from the caller;
  the library never picks a path of its own.

  Scope matters here: a template path that lives in `global:` must be
  checked **once** against the global config, not once per VDOM, or a
  single global misconfiguration is reported N times and `system/global`
  looks "missing" from every VDOM. Which paths those are is *derived from
  the normalized file itself* -- if a path is present under `global:`, it
  is global -- rather than re-read from `configuration/sections.yaml`.
  That needs no second input and cannot drift out of step with the file
  being checked, the same way `discover_scope_dirs` reads what is on disk
  instead of taking a declared list. Keep `vdom` on `check_section` and on
  all three finding types for reporting, and pass `"global"` for global
  paths so findings still say where they came from.

### `ComplianceResult` and the finding types

Every finding carries its own full coordinates, so the result is **three
flat lists**, not a nested tree:

```python
@dataclass(frozen=True)
class Violation:
    vdom: str                    # a VDOM name, or "global"
    path: str                    # cmdb path, e.g. "firewall/policy"
    object_key: Optional[str]    # None for a singleton section's own field
    field: Tuple[str, ...]       # path segments below the object
    expected: str
    actual: str

@dataclass(frozen=True)
class MissingKey:
    vdom: str
    path: str
    object_key: Optional[str]
    field: Tuple[str, ...]       # empty when the whole object is missing
    expected: Any                # scalar, or the mapping that was expected

@dataclass(frozen=True)
class UnknownPath:
    vdom: str
    path: str

@dataclass
class ComplianceResult:
    host: str
    violations: List[Violation] = field(default_factory=list)
    missing: List[MissingKey] = field(default_factory=list)
    unknown: List[UnknownPath] = field(default_factory=list)

    @property
    def is_compliant(self) -> bool:
        return not self.violations and not self.missing
```

Flat rather than nested because the findings already hold `vdom`, `path`,
and `object_key` -- a `Dict[vdom][path]` tree would encode the same
coordinates twice and let the two disagree. It also matches what the
statuses section already assumed ("the summary counts three lists"), makes
`is_compliant` and the counts trivial, and means a future fleet rollup
concatenates lists instead of merging trees. The cost is that
`to_diff_mapping` must group on the way out rather than walking a
ready-made tree -- one pass with `setdefault`, and the grouping is exactly
the nesting the diff wants.

`UnknownPath` deliberately has no `object_key` or `field`: nothing below
the path was examined, so there are no coordinates to report. Note
`is_compliant` ignores `unknown`, which is what makes UNKNOWN-only exit 0.

**`check_section` returns a plain `List[Violation | MissingKey]`, not a
`SectionCheckResult`.** An earlier draft named that wrapper, but with
coordinates on the findings it would carry `path` and `vdom` its contents
already carry, and `check_template` would immediately unwrap it. There is
nothing for it to hold that the findings do not.

**Rendering.** `to_diff_mapping` walks the three lists and inserts each
finding at `[scope][path][object_key][*field]`, where `scope` is
`"global"` for global findings and `vdoms/<vdom>` otherwise:

- `Violation` -> `{"expected": ..., "actual": ...}`
- `MissingKey` with a non-empty `field` -> `{"expected": ...}`, no `actual`
- `MissingKey` with an empty `field` -> `_status: missing` on the object
- `UnknownPath` -> `_status: unknown` on the path

`object_key` being `Optional` is what lets `system/global`'s own fields and
`firewall/address`'s objects share one insertion routine: when it is
`None`, that coordinate is simply skipped.

### Comparison semantics

Settled against the real `data/normal/fw1.yaml`, not assumed:

- **Scalars compare as `str` on both sides.** The API is inconsistent about
  types within a single file: `admintimeout: 30` is an int while
  `purdue-level: '3'` and `session-ttl: '0'` are strings -- 107 string-typed
  numbers against 368 real ints. Coercing *numerically* would collapse
  `diffservcode-forward: '000000'` to `0` and silently match a template
  written as `0`, which is wrong and invisible. `str()` fails such a case
  loudly instead, and the template author quotes the value. There are no
  booleans anywhere -- FortiOS uses `enable`/`disable` strings -- so the
  usual `True`/`"true"` trap does not exist.

  This sits slightly against "canonical form before comparison": the
  checker is a second place that knows the API lies about types. It stays
  here because the normalizer cannot fix it without data loss (there is no
  coercion that repairs `'3'` and preserves `'000000'`). Keep it in one
  named function; if config generation ever needs the same coercion, that
  is the signal to move it down into the normalizer.

- **List-valued fields compare as sets.** After normalization every list is
  either empty or a collapsed reference list (`srcintf: ['wan1']`,
  `service: ['HTTPS']`) -- `normalize_value` turns lists of objects into
  mappings, so no list of dicts survives. None of them are order-sensitive;
  they are all membership. (Policy *evaluation* order is the order of the
  `firewall/policy` mapping itself, not of any field inside a policy.)

- **Nested values recurse.** A field's value can itself be a mapping:
  `system/ntp` holds `ntpserver`, keyed by id (`1`) exactly as
  `firewall/policy` is keyed by mkey. `normalize_value` recurses, so every
  level has the same shape, and `check_object` recursing is the same code
  applying to the same structure one layer down -- not a special case. It
  also keeps `str()` key coercion working at every depth. Whole-dict `==`
  was rejected: it would break the subset rule exactly where it is least
  obvious (asserting one field of an `ntpserver` row would mean
  reproducing all eight) and turn the diff leaf into two blobs to
  eyeball-diff.

  So a finding's `field` is a **tuple of path segments**, not a dotted
  string. This is forced by the data, not style: 25 names in `fw1.yaml`
  contain a dot -- `h_fw01-10.10.100.1`, `fqdn_api.cloudflare.com`,
  `gmail.com` -- and they are all keys of the nested mappings being
  recursed into. `"member.h_fw01-10.10.100.1.subnet"` splits into six
  segments, three of them garbage; the tuple is always three. Same test
  the `_` prefix passed and the dot fails. Only `field` becomes a tuple:
  `vdom`, `path`, and `object_key` stay separate fields on the finding.

- **Two authoring bugs raise rather than becoming findings.** A rendered
  template value that is a `bool` cannot have come from FortiOS (there are
  no booleans in the data), and a kind mismatch -- template scalar against
  an actual mapping, or the reverse -- is structurally impossible for any
  firewall state to satisfy. Neither is a fact about the firewall, so
  neither belongs in a diff that describes the firewall's compliance;
  `str()`-ing them produces a stringified dict that reads as a firewall
  problem. Both live in the one named coercion function, alongside
  `StrictUndefined`: same "fail loudly rather than substitute silently"
  position. Cost accepted: one bad template line aborts that host's check
  rather than degrading to one finding, which is right for a git-tracked,
  reviewed template.

- **Object keys compare as `str`.** `firewall/policy` is keyed by int
  (`4`, `29`), `firewall/address` by string, in the same file. A template
  written with `"1":` instead of `1:` would otherwise report a spurious
  missing object with no hint why. Stringify keys on both sides before
  lookup.

### Result statuses

Three outcomes, kept distinct, as **three dataclasses** -- `Violation`,
`MissingKey`, `UnknownPath` -- not one `Finding` with a status enum. Each
carries exactly the fields it can have: `MissingKey` has no `actual`,
`UnknownPath` has no object key or field. This is structural, not
stylistic: the diff format encodes MISSING as the *absence* of the
`actual:` key, so if `actual` were an `Optional[str]` on a shared
dataclass, "missing" and "actual is None" would be the same runtime value
and the renderer could not tell them apart -- the precise failure the
format was designed around. Three types make it unrepresentable. The cost
is that the summary counts three lists instead of one `Counter`, and a
fourth status means a new class plus a renderer branch; a fourth status
should be a deliberate act.

- **FAIL** -- the value was checked and is wrong.
- **MISSING** -- the template expects a key the firewall does not have, at
  **any depth**: a policy, a field, a nested `ntpserver` row. Under
  uniform recursion these are one situation, and an absent `admintimeout`
  genuinely is missing rather than wrong. Splitting it by whether the
  expected value is a mapping would re-introduce the type-sniffing that
  `check_section` just removed.
- **UNKNOWN** -- the template names a path that was never exported, so
  nothing was checked. A path that *was* exported but came back empty is
  not UNKNOWN; its objects are MISSING (see "Checking every VDOM"). Collapsing this into FAIL makes a broken *export*
  look like a broken *firewall*, which sends you debugging the wrong
  thing. Across a fleet, a host not yet exported is routine rather than an
  error, so a script exits 0 on UNKNOWN alone (see `check_fw1.py`).

### Diff output

The artifact is `data/diff/<host>.yaml`. Writing it rather than only
printing makes stage 3 a real pipeline stage: a fleet rollup or dashboard
can be built against fixed input without re-running the check.

The document follows the *normalized* shape -- `host`, `global:` as a
sibling of `vdoms:`, then cmdb path, object key, field -- so a finding
sits at the exact path you would navigate to in `data/normal/<host>.yaml`.
Only the leaf differs: instead of a value, an `expected`/`actual` pair.

```yaml
host: fw1
global:
  system/global:
    admintimeout:
      expected: '15'
      actual: '30'
vdoms:
  root:
    firewall/policy:
      1:
        logtraffic:
          expected: all
          actual: utm
    firewall/address:
      hq-lan:
        _status: missing
        subnet:
          expected: 192.168.1.0/24
```

Only findings appear. A compliant field is not written: the template
already records what was expected of it, and the normalized file sits
next to this one if the full picture is wanted.

MISSING and UNKNOWN encode *structurally*, not as sentinel values. For a
missing object the `actual:` key is simply absent -- there was no value,
so none is written. `actual: null` or `actual: missing` would be a
plausible-looking wrong answer, and `missing` is a legal FortiOS string.
UNKNOWN attaches at path level, where it belongs, since nothing below it
was checked -- and **per VDOM**, inside `vdoms:` like any other finding:

```yaml
vdoms:
  root:
    system/ntp:
      _status: unknown
```

Per-VDOM rather than hoisted to the top of the document, because
`exporter.py:213` records fetch failures per `(vdom, path)`: a section can
genuinely be present in `root` and absent in `blulab` on the same host,
and that asymmetry is the case most worth seeing -- it means one fetch
failed, not that the template is wrong. It also follows from the scope
rule: globalness is *derived from presence* under `global:`, and a path
that was never exported is present nowhere, so the checker cannot know
whether it was meant to be global or vdom-scoped. It can only report
where it looked and did not find it. Hoisting would require exactly the
claim it lacks the data for. A path missing from every VDOM therefore
produces N identical entries -- honest, since N VDOMs really were
unchecked, and the same fan-out already accepted for vdom-scoped
violations. The consequence to live with: a genuinely global path that
was never exported is reported once per VDOM rather than once under
`global:`.

The `_` prefix cannot collide with a real key. Since MISSING attaches at
any depth, `_status` is no longer confined to field level -- it can sit at
object level (`hq-lan` above) or inside a nested row -- so the guarantee
has to hold everywhere, not one layer down. It does: of the 522 distinct
keys at every depth in `fw1.yaml`, none begins with an underscore. Object
names contain them internally (`h_fw01-10.10.100.1`), which is harmless;
what matters is that none *starts* with one.

A clean run still writes the file, carrying `host:` and nothing else.
"Checked, compliant" and "never ran" must not look alike.

The extension is `.yaml`, not `.diff`: the content is YAML and editors
should highlight it as such. The directory name already says what it is.

### Checking every VDOM

A vdom-scoped path in the template is a fleet-wide assertion by
construction. `check_template` is a cross product of every VDOM present in
the normalized file with every vdom-scoped path in the template -- the
same shape as `build_export_plan` in the exporter. Nothing in the template
declares the loop; the loop *is* the semantics, and a violation appears
once per VDOM under its own key.

The complement is what keeps that readable: a global path is checked
once, against `global:`. Without that routing the same loop would report
one global misconfiguration N times and bury the per-VDOM findings.

Two things this deliberately does not do:

- **Consistency assertions** ("every VDOM must have the *same* value,
  whatever it is") are a different kind of rule -- relational rather than
  desired-state, with no `expected:` to put in the diff. They would need
  their own template construct and result type.
- **Per-VDOM exceptions** ("this value, but only in `dmz`") are the
  overlay problem, and belong to template composition rather than to the
  checker. fw1 does now have two VDOMs, so this is closer than it was;
  it stays out of scope here because the checker consumes an
  already-composed template either way.

**Validated against real data.** fw1 now exports two VDOMs (`root`,
`blulab`) plus `global`: the exporter fans out per VDOM, `global` is
fetched once, and the normalized file carries `global:` as a sibling of a
two-entry `vdoms:`. The shape stage 3 routes on is confirmed, not assumed.

That export also exposed a normalizer bug, since fixed: `blulab` has zero
policies, and an empty table used to normalize to `[]` while a populated
one became a mapping -- so a keyed lookup raised `AttributeError` on
exactly the VDOM a compliance check has the most to say about. Empty
tables are now `{}` (see stage 2).

An empty table is **MISSING, not UNKNOWN**. The section was exported
successfully and genuinely has no rows: that is the firewall's state, not
a gap in the export. So a template declaring `firewall/policy: {1: ...}`
against `blulab` reports object 1 missing, and must not report the path
unknown. UNKNOWN is reserved for a path that never appears in the
normalized file at all.

### `scripts/check_fw1.py`

Loads `vars/fw1.yaml` + `templates/baseline.yaml`, renders, checks against
`data/normal/fw1.yaml`, writes `data/diff/fw1.yaml`, prints a one-line
summary (counts of `FAIL`/`MISSING`/`UNKNOWN` and the path written), and
exits 1 if non-compliant (same convention as `export_fw1.py`). The detail
lives in the file; stdout only says what happened and where to look.

Exit codes: **1 on any FAIL or MISSING; UNKNOWN alone exits 0**, since an
unexported path is an export gap rather than a firewall fault, and across
a fleet a not-yet-exported host is routine.

This is not as lax as it looks: `export_fw1.py` already returns
`1 if result.failures else 0`, so a section that stops fetching fails the
*export* run loudly, naming the vdom, path, and API error. Exiting 0 on
UNKNOWN-only is therefore not ignoring the problem -- it is declining to
re-report an upstream failure that already has an owner and a better error
message. The `UNKNOWN: 3` count in the summary remains the local tripwire.
If CI ever needs to distinguish the two, a distinct exit 2 for
UNKNOWN-only is the upgrade.

A missing `data/normal/<host>.yaml` is not a compliance outcome at all:
`load_normalized` raises, the script catches it and exits 1 pointing at
`normalize_fw1.py`, and a later fleet loop catches it per iteration and
marks that host errored.

Mirrors `export_fw1.py` throughout: the template, vars, and diff-output
paths are module constants anchored to `REPO_ROOT`, exactly as
`SECTIONS_FILE` and `INVENTORY_FILE` are there, and the script -- not the
library -- decides what to print, where to write, and what to exit with. A host-to-template mapping file is
deliberately *not* built yet: while each script names its one host, it
would have no caller. Add it with the fleet script, where a loop needs to
resolve templates for hosts it did not name individually.

### Fleet exit-code policy

Settled, for whenever the fleet script is written. All hosts are checked
and summarized; per-host errors are caught per iteration rather than
aborting, so one failure cannot hide the remaining hosts. A host that
raises (never normalized) is neither compliant nor non-compliant -- the
same trichotomy as FAIL/MISSING/UNKNOWN, one level up.

**Errored hosts do not fail the run, but zero successfully-checked hosts
does.** If `normal_dir` is misconfigured or empty, every host raises,
nothing is checked, and the summary would otherwise report a green run
that verified nothing. Unlike UNKNOWN there is no upstream red run to
catch that, because a bad `normal_dir` never touches stage 1 or 2. "I
checked nothing" is a distinct failure from "I checked things and one was
unchecked". Matching `export_fw1.py`'s any-failure-is-1 was rejected: the
exporter talks to appliances, where a failure means something broke now,
while a fleet check reading disk will routinely find hosts mid-onboarding,
and making that red trains people to ignore red.

This lives in the fleet script only; the library has no notion of it.

### Out of scope: which template gets checked against which firewall

The checker takes **one rendered template and one normalized host**, and
produces one diff. It has no notion of model, role, site, or firmware, and
never selects or composes a template. That logic is a separate concern,
built later, and it needs no change here -- the rendered template is
already a parameter.

Recorded as context for whoever builds it: heterogeneity is **two axes**,
and solving both with one mechanism is the trap to avoid.

- **Model capability** -- *can* this box have this path? A path
  unsupported on a model fails to fetch, is recorded in `FailedSection`,
  is never written, and is therefore absent from the normalized file --
  which is exactly UNKNOWN. So model differences already degrade to "could
  not check" rather than "firewall is broken", with no new construct. No
  overlay can fix a box that lacks a feature: there is no correct
  `expected:` value.
- **Role / use-case** (branch vs DC) -- what *should* this box's values
  be? A composition problem, solved by a module that merges templates into
  one dict before the checker sees it.

The one cost to watch: on a small model checked against a large baseline,
the standing block of UNKNOWNs never resolves, and UNKNOWN stops reading
as a signal. That is a reporting concern for fleet tooling, not a checker
change.

Template versioning by FortiOS release stays open and largely dissolves if
default suppression lands, since defaults are the main thing that differs
between versions.

### Decisions already made

- Rules are one parameterised desired-state template per firewall
  type/baseline, not a rules file disconnected from provisioning -- the same
  template can later drive pushing config to a new firewall.
- Checks run against exported-then-normalized config, not live against the
  API.
- Matching is subset: everything the template declares must be present and
  correct; extra objects and fields on the firewall are ignored.
- **One template per firewall**, and the rendered template is a parameter.
  Combining a baseline with role overlays is out of scope here; it is a
  separate module that produces a template dict, which this checker
  consumes unchanged.
- **One firewall per call.** Fleet handling is a loop in a script, matching
  stages 1 and 2. `ComplianceResult` carries `host` so that loop is cheap
  to add later.
- Comparison semantics, statuses, and scope routing: see the sections
  above.
- **The output is a written diff**, `data/diff/<host>.yaml`, in the
  normalized shape with `expected`/`actual` leaves. It is a *renderer*
  over `ComplianceResult`, not a replacement for it: the dataclass tree
  stays, `to_diff_mapping` is a pass over it, and a later JSON or stdout
  format is a third renderer rather than a rewrite.
- **A vdom-scoped template path is checked in every VDOM.** Fleet-wide
  assertions need no new syntax; per-VDOM exceptions and cross-VDOM
  consistency rules are out of scope.
- **Comparison recurses to any depth**, with `field` as a tuple of path
  segments; no table-vs-singleton dispatch, since after normalization
  every level is the same shape.
- **Three result dataclasses**, not one with a status enum; MISSING means
  an absent key at any depth.
- **`ComplianceResult` is three flat lists**, since findings carry their
  own coordinates; `to_diff_mapping` groups on the way out. No
  `SectionCheckResult` wrapper.
- **UNKNOWN is per-VDOM**, never hoisted.
- **Authoring bugs raise; only firewall state becomes a finding.**
  Undefined vars, bool-valued template values, and scalar/mapping kind
  mismatches all fail at check or render time.
- **The checker reads a `NormalizedHost`, never the document's keys.**
  `from_mapping` joins `to_mapping` in the normalizer.
- **Exit 1 on FAIL/MISSING, 0 on UNKNOWN alone**; a missing normalized
  file raises rather than checking nothing.
- **Fleet: errored hosts do not fail the run; checking zero hosts does.**
- **Template selection and composition are out of scope** -- the checker
  knows nothing of model, role, or site.

## Dependencies

`requirements.txt` has `requests`, `urllib3`, `PyYAML`, `Jinja2`. The
normalizer uses PyYAML only; Jinja2 is for stage 3.
