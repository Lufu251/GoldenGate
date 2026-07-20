# GoldenGate

Define what a FortiGate's configuration *should* look like, then check what it
actually looks like against that.

The tooling reads config off an appliance over the REST API, rewrites it into a
diff-friendly canonical form, and compares it against a parameterised
desired-state template. The result is a YAML diff naming exactly what is wrong,
what is missing, and what could not be checked.

## Pipeline

Three stages, each writing its output to disk so the next can be built and
tested against fixed input without re-running the one before it.

```
appliance --export----> data/raw/<host>/<scope>/cmdb/<path>.json
          --normalize-> data/normal/<host>.yaml
          --check-----> data/diff/<host>.yaml
```

`<scope>` is either a VDOM name or `global`. In multi-VDOM mode some config
(`system/global`, `system/ntp`) exists once for the whole appliance rather than
per-VDOM, so it is exported once and normalized into a top-level `global:` key
that is a *sibling* of `vdoms:`, not an entry inside it.

## Getting started

```bash
pip install -r requirements.txt
cp inventory.yaml.example inventory.yaml   # then fill in your firewalls
```

`inventory.yaml` holds live REST API tokens and is gitignored. Create a REST API
admin on the FortiGate and generate a token for it; authentication is Bearer.

Then run the three stages in order:

```bash
python3 scripts/export_fw1.py      # appliance  -> data/raw/fw1/
python3 scripts/normalize_fw1.py   # data/raw/  -> data/normal/fw1.yaml
python3 scripts/check_fw1.py       # + template -> data/diff/fw1.yaml
```

Each script handles one host (`fw1`) and exists to show the stage end to end.
Fleet loops are deliberately not built yet — see `plans/`.

### Exit codes

| Script | 0 | 1 |
| --- | --- | --- |
| `export_fw1.py` | every declared section fetched | any section failed to fetch |
| `normalize_fw1.py` | wrote the normalized file | nothing found under `data/raw/<host>/` |
| `check_fw1.py` | compliant, or UNKNOWN findings only | any FAIL or MISSING, or the host was never normalized |

`check_fw1.py` exits 0 on UNKNOWN alone because an unexported path is a gap in
stage 1, and `export_fw1.py` already fails its own run loudly for it — naming
the VDOM, path, and API error. Re-reporting it downstream would only be a worse
error message. The `UNKNOWN` count in the summary line stays as the tripwire.

## Stage 1 — export

Reads the declared config sections off the appliance and writes each response
envelope to disk unchanged.

Which sections to fetch, and the scope each lives in, is declared in
`configuration/sections.yaml`. Scope is a property of the FortiOS data model
rather than of your appliance — it cannot be discovered over the API, so it is
declared. It does shift between FortiOS versions, so revisit that file after a
firmware upgrade.

A section that fails to fetch (typically: unsupported on this model) is recorded
and skipped rather than aborting the export. Files are laid out one directory
per cmdb path segment, not flattened into a single name: path segments contain
`-` themselves, so `cmdb-system-dns-database.json` would read back ambiguously
as `system/dns/database`.

## Stage 2 — normalize

Rewrites the raw JSON into one YAML file per firewall:

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

The raw API responses are faithful but hostile to comparison. Normalizing:

- drops envelope metadata and per-object bookkeeping (`q_origin_key`, `uuid`),
- collapses reference lists — `[{"name": "wan1", "q_origin_key": "wan1"}]`
  becomes `["wan1"]`,
- turns tables into mappings keyed by their mkey, so a diff stays local to the
  object that actually changed instead of shifting every following line when one
  policy is inserted,
- lifts appliance facts to the top and drops the `cmdb/` prefix,
- preserves API order, which for `firewall/policy` is the firewall's real
  evaluation order and therefore part of the config's meaning.

The mkey is *inferred from the data* — FortiGate echoes each object's identity
in `q_origin_key`, so the mkey is whichever field holds that same value. No
lookup table and no schema call.

An empty table normalizes to `{}`, not `[]`, so a section's type does not depend
on whether the firewall happens to have rows in it. Nesting is uniform: an
`ntpserver` row inside `system/ntp` is keyed by `id` exactly as
`firewall/address` is keyed by `name`, so every level has the same shape.

## Stage 3 — check

Compares the normalized config against a rendered desired-state template.

`templates/baseline.yaml` is keyed by cmdb path, with Jinja2 placeholders filled
from `vars/<host>.yaml`:

```yaml
system/global:
  hostname: "{{ hostname }}"
  admintimeout: {{ admin_timeout }}

firewall/policy:
  4:
    action: accept
    srcintf: [wan1]
```

`vars/<host>.yaml` is a flat mapping of name → value, tracked in git — there are
no secrets in it, those stay in the gitignored `inventory.yaml`.

**Matching is subset.** Everything the template declares must be present and
correct; objects and fields the firewall has but the template does not are
ignored. A firewall carries hundreds of settings nobody ever chose, so asserting
on absence would mean transcribing the whole box.

**A vdom-scoped template path is checked in every VDOM.** That cross product is
the semantics — nothing in the template declares the loop. A global path is
checked once, or a single global misconfiguration gets reported N times. Which
paths are global is derived from the normalized file itself: if a path sits
under `global:`, it is global.

### Three outcomes

| | meaning |
| --- | --- |
| **FAIL** (`Violation`) | the value was checked and is wrong |
| **MISSING** (`MissingKey`) | the template expects a key the firewall does not have, at any depth |
| **UNKNOWN** (`UnknownPath`) | the path was never exported, so nothing was checked |

UNKNOWN is kept apart from FAIL so a broken *export* does not read as a broken
*firewall* and send you debugging the wrong thing. A path that *was* exported
but came back empty is not UNKNOWN — its objects are MISSING, because an empty
section is the firewall's real state rather than a gap in the export.

### Template bugs raise, they do not become findings

An undefined Jinja2 variable, a `bool` where FortiOS has only
`enable`/`disable` strings, and a scalar asserted against a mapping are all
statements about the *template*, not about the firewall. They raise rather than
being written into a diff that claims to describe the firewall's compliance.

The file is rendered as **text** and then parsed as YAML — which keeps `{% for %}`
and `{% if %}` available, but means the whole file is template source, *YAML
comments included*. A literal Jinja2 tag written in a comment is still parsed as
one. It also makes quoting the author's job: leave a placeholder unquoted and a
value of `on` or `no` becomes a bool that no FortiOS value can ever match.

### Comparison semantics

- **Scalars compare as strings on both sides.** The API is inconsistent about
  types within a single file — `admintimeout` comes back as an int while
  `purdue-level: '3'` is a string. Coercing *numerically* instead would collapse
  `diffservcode-forward: '000000'` to `0` and silently match a template written
  as `0`.
- **Lists compare as sets.** After normalization every list is a collapsed
  reference list, and none are order-sensitive.
- **Object keys compare as strings.** `firewall/policy` is keyed by int and
  `firewall/address` by string in the same file.
- **Comparison recurses to any depth**, with the same code at every level.

### The diff

```yaml
host: fw1
global:
  system/global:
    admintimeout:
      expected: '15'
      actual: '30'
vdoms:
  blulab:
    firewall/policy:
      '4':
        _status: missing
```

The document follows the normalized shape, so a finding sits at the exact path
you would navigate to in `data/normal/<host>.yaml`. Only the leaf differs:
`expected`/`actual` instead of a value. Only findings appear.

MISSING and UNKNOWN encode *structurally*: for a missing value the `actual:` key
is simply absent, because there was no value to write. `actual: missing` would
be indistinguishable from a firewall genuinely holding the string `missing`.

A clean run still writes the file, carrying `host:` and nothing else —
"checked, compliant" and "never ran" must not look alike.

## Layout

```
fortigate/
  api/         talks to the appliance
    client.py      HTTP/auth over the REST API
    inventory.py   which firewalls exist (inventory.yaml)
  config/      fetches and reshapes what the API returns
    sections.py    which sections to fetch, and their scope
    exporter.py    stage 1
    normalizer.py  stage 2
  compliance/  compares config against a template
    checker.py     stage 3
configuration/sections.yaml   what to export
templates/baseline.yaml       desired state
vars/<host>.yaml              template variables
scripts/                      one runnable per stage
data/                         generated, gitignored
plans/                        design docs for work not yet built
DECISIONS.md                  the architectural principles behind all of it
```

Packages are grouped by responsibility with one-way dependencies:
`compliance → config → api`, never the reverse. The checker never touches an
appliance — it reads the normalized YAML off disk through
`NormalizedHost.from_mapping`, so it works on a typed object and never indexes
the document by key name.

`data/` is gitignored: it holds raw appliance config, which includes hashed
admin passwords, VPN pre-shared keys, and certificates.

## What is not built yet

Fleet-wide runs, template composition and per-host selection, default
suppression, and config generation. Each has a design doc under `plans/`.
