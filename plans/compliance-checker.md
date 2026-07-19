# Compliance checker

Goal: define parameterised templates of expected firewall configuration, and
check exported config against them to see whether a firewall is compliant.

The pipeline is three stages, each writing to disk so the next can be
developed and tested on its own:

```
appliance --export--> data/raw/<host>/<scope>/cmdb-*.json
          --normalize--> data/normal/<host>.yaml
          --check--> compliance report
```

`<scope>` is either a VDOM name or `global`. In multi-VDOM mode some
config (`system/global`, `system/ntp`) exists once for the whole
appliance rather than per-VDOM, so it is exported once and normalized
into a top-level `global:` key that is a *sibling* of `vdoms:`, not an
entry inside it.

Stage 1 (`config/exporter`) and stage 2 (`config/normalizer`) are
implemented. Stage 3 is still the plan below.

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
local to the object that actually changed. Appliance facts lift to the
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
independently. Note it also largely answers the open "template versioning /
firmware differences" question below.

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

## Stage 3: compliance checker -- NOT YET IMPLEMENTED

### New files

- `templates/baseline.yaml` -- desired-state template, keyed by cmdb path.
  Jinja2 placeholders for anything parameterised.
- `vars/fw1.yaml` -- one file per firewall, flat mapping of variable name ->
  value. Tracked in git (no secrets -- those stay in the gitignored
  `inventory.yaml`).
- `fortigate/compliance/checker.py` -- the checking logic, in its own
  package alongside `api/` and `config/`. It never touches the appliance:
  it reads the normalized YAML off disk. It does import `GLOBAL_SCOPE`
  from `config/sections.py`, because it has to know which top-level key
  holds whole-appliance config and that name is defined in exactly one
  place. So `compliance -> config -> api`, one direction, no cycle.
- `scripts/check_fw1.py` -- runnable, mirroring `scripts/export_fw1.py`.

### Consequences for stage 3 of the normalizer landing

The template shape gets simpler, because two of stage 3's original open
questions are now answered by the layer beneath it:

- **`key:` is gone.** Templates no longer declare the mkey per path; the
  normalizer already keyed every table. Expected objects are written as a
  mapping keyed the same way, and matching is a dict lookup rather than a
  scan.
- **Deep/nested comparison is mostly gone.** The original worry was
  comparing `srcintf: [{"name": "port1"}]`; normalized, that's
  `srcintf: ["port1"]` and a plain `==` works. Type coercion (the API
  returning numbers as strings) may still need handling.
- **`load_exported_section` targets `data/normal/<host>.yaml`**, not
  `data/raw`. It reads one file per host rather than reassembling many.

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
combining several templates into one -- if that is ever needed -- becomes
a separate module producing a template dict, with no change here.

- `load_vars(path) -> dict`
- `render_template(path, variables) -> dict` -- Jinja2-render the text, then
  `yaml.safe_load`. Uses `StrictUndefined`: an undefined `{{ var }}`
  renders as an empty string by default, which silently asserts the wrong
  expected value. Failing at render time is the only safe behaviour.
- `load_normalized(normal_dir, host_name) -> NormalizedHost`
- `check_object(expected, actual, ...) -> List[Violation]` -- per-field
  subset comparison, using the semantics below.
- `check_section(path, expected, actual, vdom) -> SectionCheckResult` --
  dict-vs-mapping dispatch; `Violation`s for mismatched fields,
  `MissingObject`s for objects the template expects but the firewall lacks.
  Objects present on the firewall but absent from the template are ignored.
- `check_template(template, normal_dir, host_name) -> ComplianceResult` --
  every VDOM x every path in the template. Carries `host`, so a script
  looping over several firewalls can print and aggregate without threading
  identity alongside the result.

  Scope matters here: a template path that lives in `global:` must be
  checked **once** against the global config, not once per VDOM, or a
  single global misconfiguration is reported N times and `system/global`
  looks "missing" from every VDOM. Which paths those are is *derived from
  the normalized file itself* -- if a path is present under `global:`, it
  is global -- rather than re-read from `configuration/sections.yaml`.
  That needs no second input and cannot drift out of step with the file
  being checked, the same way `discover_scope_dirs` reads what is on disk
  instead of taking a declared list. Keep `vdom` on
  `check_section`/`Violation` for reporting, and pass `"global"` for
  global paths so violations still say where they came from.

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

- **Object keys compare as `str`.** `firewall/policy` is keyed by int
  (`4`, `29`), `firewall/address` by string, in the same file. A template
  written with `"1":` instead of `1:` would otherwise report a spurious
  missing object with no hint why. Stringify keys on both sides before
  lookup.

### Result statuses

Three outcomes, kept distinct:

- **FAIL** -- the field was checked and is wrong.
- **MISSING** -- the template expects an object the firewall does not have.
- **UNKNOWN** -- the template names a path that was never exported, so
  nothing was checked. Collapsing this into FAIL makes a broken *export*
  look like a broken *firewall*, which sends you debugging the wrong
  thing. Across a fleet, a host not yet exported is routine rather than an
  error, so a script may reasonably exit 0 on UNKNOWN alone.

### `scripts/check_fw1.py`

Loads `vars/fw1.yaml` + `templates/baseline.yaml`, renders, checks against
`data/normal/fw1.yaml`, prints `FAIL`/`MISSING`/`UNKNOWN` per finding,
exits 1 if non-compliant (same convention as `export_fw1.py`).

Mirrors `export_fw1.py` throughout: the template and vars paths are module
constants anchored to `REPO_ROOT`, exactly as `SECTIONS_FILE` and
`INVENTORY_FILE` are there, and the script -- not the library -- decides
what to print and what to exit with. A host-to-template mapping file is
deliberately *not* built yet: while each script names its one host, it
would have no caller. Add it with the fleet script, where a loop needs to
resolve templates for hosts it did not name individually.

### Decisions already made

- Rules are one parameterised desired-state template per firewall
  type/baseline, not a rules file disconnected from provisioning -- the same
  template can later drive pushing config to a new firewall.
- Checks run against exported-then-normalized config, not live against the
  API.
- Matching is subset: everything the template declares must be present and
  correct; extra objects and fields on the firewall are ignored.
- **One template per firewall**, and the rendered template is a parameter.
  Combining a baseline with role overlays is out of scope here; if it is
  ever needed it becomes a separate module that produces a template dict,
  which this checker consumes unchanged.
- **One firewall per call.** Fleet handling is a loop in a script, matching
  stages 1 and 2. `ComplianceResult` carries `host` so that loop is cheap
  to add later.
- Comparison semantics, statuses, and scope routing: see the sections
  above.

### Decisions that still need to be made

- **Report format**: plain stdout lines vs. structured JSON/YAML for feeding
  a dashboard later. Starting with stdout; keep `ComplianceResult` a clean
  dataclass tree and printing a separate function over it, so JSON becomes
  a second renderer rather than a rewrite.
- **Fleet exit-code policy**: when a script does loop, does one
  non-compliant firewall fail the run, or are all hosts checked and
  summarized? Almost certainly the latter -- one failure must not hide the
  remaining hosts -- which means per-host errors are caught per iteration
  rather than aborting.
- **Template versioning / firmware differences**: one template per baseline,
  or do they vary by FortiOS version or model? Largely dissolves if default
  suppression lands, since defaults are the main thing that differs between
  versions.

## Dependencies

`requirements.txt` has `requests`, `urllib3`, `PyYAML`, `Jinja2`. The
normalizer uses PyYAML only; Jinja2 is for stage 3.
