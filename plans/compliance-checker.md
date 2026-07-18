# Compliance checker

Goal: define parameterised templates of expected firewall configuration, and
check exported config against them to see whether a firewall is compliant.

The pipeline is three stages, each writing to disk so the next can be
developed and tested on its own:

```
appliance --export--> data/raw/<host>/<vdom>/cmdb-*.json
          --normalize--> data/normal/<host>.yaml
          --check--> compliance report
```

Stage 1 (`config_exporter`) and stage 2 (`normalizer`) are implemented.
Stage 3 is still the plan below.

---

## Stage 2: normalizer -- IMPLEMENTED

Raw API responses are faithful but hostile to comparison: every object
carries a `q_origin_key` duplicate of its own identity, tables come back as
lists (so inserting one policy shifts every following line of a diff), and
envelope metadata like `revision` changes on every export even when nothing
was configured.

### Files

- `fortigate/normalizer.py` -- the transform.
- `scripts/normalize_fw1.py` -- runnable, mirrors `scripts/export_fw1.py`.

### Output shape: `data/normal/<host>.yaml`

```yaml
host: fw1
serial: FGT70FTK22034248
version: v7.4.12
build: 2902
vdoms:
  root:
    system/ntp:
      ntpsync: enable
      ntpserver:
        1:
          server: ch.pool.ntp.org
    firewall/policy:
      4:
        name: wan-dmz-rproxy
        srcintf: [wan1]
        dstintf: [vlan120]
        action: accept
```

One file per firewall, VDOMs nested inside. `serial`/`version`/`build` are
identical in every envelope, so they lift to the top instead of repeating
per section. The `cmdb/` prefix drops -- every exported section shares it.

### The four transforms

1. **Strip the envelope** -- keep `results`, drop `revision`, `size`,
   `matched_count`, `next_idx`, `http_method`, ... which change between
   exports without any config change.
2. **Drop noise fields** -- `q_origin_key`, `uuid`, `uuid-idx`: firewall
   bookkeeping, not operator intent.
3. **Collapse reference lists** -- `[{"name": "wan1", "q_origin_key":
   "wan1"}]` -> `["wan1"]`. Only when every element's keys are a subset of
   `{name, q_origin_key}`, so real nested tables aren't flattened by
   accident.
4. **Tables to mkey-keyed maps** -- a list of objects becomes a mapping. This
   is what makes diffs local: changing policy 29 touches only policy 29's
   lines, instead of shifting everything after it.

All four are one recursive function, so nested tables get the same treatment
as top-level ones -- `ntpserver` inside `system/ntp` is keyed by `id`
exactly as `firewall/address` is keyed by `name`.

### Decisions made here

- **mkey is inferred from the data, not configured.** FortiGate echoes each
  object's identity in `q_origin_key`, so the mkey is whichever field holds
  that same value. Candidates are intersected across every row and `name` is
  preferred on a tie. No per-path lookup table, no schema call, and it works
  for cmdb paths nobody has hardcoded yet.
- This removes the compliance template's `key:` field entirely -- see
  "Consequences for stage 3" below.
- **A singleton is just a table-shaped thing with no `q_origin_key`**, so
  `system/global` falls through as a plain dict with no special-casing.
- **The mkey is removed from the row body** once it becomes the map key --
  otherwise it lives in two places and they can disagree.
- **Order is whatever the API returned**, and `yaml.safe_dump(...,
  sort_keys=False)` preserves it. For `firewall/policy` that is the
  firewall's real evaluation order. This is load-bearing, not cosmetic:
  PyYAML sorts keys by default, which would silently reorder policies into
  `12, 4, 7` and produce a subtly wrong firewall downstream with no visible
  sign it happened.
- **VDOMs and sections are read from disk**, not from a caller-supplied
  list, so the normalizer stays correct as the export grows.

### Verified against the real fw1 export

35 policies in / 35 out and 26 addresses in / 26 out (no mkey collisions
silently dropping objects); order preserved exactly, including the
non-sequential `4, 29, 50, 32, ...` that confirms it is evaluation order
rather than sorted-by-id; `policyid` / `name` / `id` all inferred correctly.

---

## Deliberately left out of the normalizer

Each of these was considered and postponed, not overlooked.

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

### A uniqueness check on inferred mkeys

`infer_mkey` is inference, so it can be wrong in a way a hardcoded table
cannot. The failure mode: a row where some other field coincidentally equals
the identity -- an address object literally named `dynamic` makes both `name`
and `type` candidates. The `name`-preference rule covers that case but not
one where `name` isn't the real mkey. Cheap guard when wanted: assert the
resulting map has the same length as the input list, since a collision means
the inference was wrong and objects were silently dropped. Confirmed by hand
on the current export; not enforced in code.

### An explicit `_order:` marker on ordered tables

API order is the real order and is preserved, so this is not needed for
correctness today. The argument for adding it later is that the invariant is
currently *implicit*: nothing in the YAML says policy order is semantic, so
a future formatter, merge resolver, or someone alphabetizing by hand can
destroy it invisibly. An explicit `_order` list makes it checkable on load.
It should apply only to known-ordered paths -- most tables (`firewall/address`)
have no meaningful order, and unlike the mkey this cannot be inferred from
the data; it's semantics, not structure.

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

### `data/normal/` is gitignored

The normalized YAML is the artifact you'd most want diffed in git, which is a
real argument for tracking it. It stays ignored anyway because exported
firewall config contains secrets -- hashed admin passwords, VPN pre-shared
keys, certificates -- straight off the appliance. Revisit only alongside a
scrubbing pass.

---

## Stage 3: compliance checker -- NOT YET IMPLEMENTED

### New files

- `templates/baseline.yaml` -- desired-state template, keyed by cmdb path.
  Jinja2 placeholders for anything parameterised.
- `vars/fw1.yaml` -- one file per firewall, flat mapping of variable name ->
  value. Tracked in git (no secrets -- those stay in the gitignored
  `inventory.yaml`).
- `fortigate/compliance.py` -- the checking logic.
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

### `fortigate/compliance.py`

Dataclasses plus small pure functions, same style as `config_exporter.py`
and `normalizer.py`.

- `load_vars(path) -> dict`
- `render_template(path, variables) -> dict` -- Jinja2-render the text, then
  `yaml.safe_load`.
- `load_normalized(normal_dir, host_name) -> NormalizedHost`
- `check_object(expected, actual, ...) -> List[Violation]` -- per-field
  subset comparison.
- `check_section(path, expected, actual, vdom) -> SectionCheckResult` --
  dict-vs-mapping dispatch; `Violation`s for mismatched fields,
  `MissingObject`s for objects the template expects but the firewall lacks.
  Objects present on the firewall but absent from the template are ignored.
- `check_template(template, normal_dir, host_name) -> ComplianceResult` --
  every VDOM x every path in the template.

### `scripts/check_fw1.py`

Loads `vars/fw1.yaml` + `templates/baseline.yaml`, renders, checks against
`data/normal/fw1.yaml`, prints `FAIL`/`MISSING` per violation, exits 1 if
non-compliant (same convention as `export_fw1.py`).

### Decisions already made

- Rules are one parameterised desired-state template per firewall
  type/baseline, not a rules file disconnected from provisioning -- the same
  template can later drive pushing config to a new firewall.
- Checks run against exported-then-normalized config, not live against the
  API.
- Matching is subset: everything the template declares must be present and
  correct; extra objects and fields on the firewall are ignored.

### Decisions that still need to be made

- **Template-to-firewall mapping**: how does a firewall know which
  template(s) apply? A field in `inventory.yaml`, a naming convention
  (`templates/<name>.yaml` matching inventory `name`), or an explicit list in
  the script/CLI.
- **Multiple firewalls at once**: is a fleet-wide runner needed (loop over
  inventory, aggregate one report), and is "done" a CLI, a script per host,
  or both?
- **Value comparison semantics**: nesting is largely handled by
  normalization, but the API can still return numbers as strings. Is exact
  `==` acceptable to start, or is coercion needed?
- **Unrendered/undefined variables**: Jinja2 renders an unknown `{{ var }}`
  as an empty string by default, silently producing a wrong expected value.
  Fail loudly at render time instead (`StrictUndefined`)?
- **Section never exported**: if `data/normal/<host>.yaml` has no entry for a
  path the template expects, is that a violation or a distinct "unknown"
  status separate from "non-compliant"?
- **Report format**: plain stdout lines vs. structured JSON/YAML for feeding
  a dashboard later.
- **Template versioning / firmware differences**: one template per baseline,
  or do they vary by FortiOS version or model? Largely dissolves if default
  suppression lands, since defaults are the main thing that differs between
  versions.

## Dependencies

`requirements.txt` has `requests`, `urllib3`, `PyYAML`, `Jinja2`. The
normalizer uses PyYAML only; Jinja2 is for stage 3.
