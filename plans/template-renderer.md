# Template renderer

Goal: split `fortigate/compliance/checker.py` into two modules — one that
turns template files into a desired-state document, one that compares that
document against a firewall.

Today the checker does both. Rendering (`load_vars`, `render_template`) sits
in the same module as comparison, and `TemplateError` is raised from inside
the comparison walk. That conflates two questions that are answered against
different inputs: *is the template well-formed* needs only the template,
while *does the firewall match* needs both sides.

The split also unblocks per-host template selection: not every firewall gets
the same policy, and a host that needs a baseline plus a role template needs
those merged before anything is compared.

---

## Target shape

```
templates/*.yaml + vars/<host>.yaml
          --render--> one desired-state doc per template
          --merge-->  one desired-state doc
          --check-->  data/diff/<host>.yaml
```

`fortigate/compliance/template.py` owns everything left of `check`.
`fortigate/compliance/checker.py` owns `check` and below.

**Neither module imports the other.** `scripts/check_fw1.py` composes them.
That is stronger than a one-way arrow: there is no arrow.

---

## Concerns

Three decisions below are contested rather than obvious. They are collected
here so a reviewer meets them before the detail; each is argued in place in
the section it belongs to.

### 1. One arrow survives the split

The no-arrow property has a single exception: `checker.py` imports
`TemplateError` from `template.py`, for the mapping-vs-scalar mismatch.

That raise site cannot move. Knowing the template expects a mapping where
the firewall holds a scalar requires the firewall's value, so it is detected
at comparison time even though it is a template fault. The alternatives are
worse: duplicating the exception type gives two names for one condition, and
defining it in a third module adds a package so one class has a neutral home.

The bend is to an imported exception type, not to behaviour. See §2.

### 2. An empty `templates:` refuses to write a diff

A host nobody has written a policy for and a host that passed its checks
must not produce the same-looking file (DECISIONS line 15).

The cost is that adding a firewall to `inventory.yaml` and running the full
pipeline now fails at stage 3 until templates are chosen for it. That is
intended — the failure is loud, names the host, and says what is missing —
but it does mean the check stage is no longer runnable fleet-wide by
default. `Inventory.load` deliberately does not raise, so stages 1 and 2
stay unaffected. See §3.

### 3. The inventory move is load-bearing, not tidying

`fortigate/api/inventory.py` → `fortigate/inventory.py` touches three files
that this change otherwise has no reason to open, which makes it look like
drive-by refactoring inside an unrelated plan.

It is not optional. Template selection belongs in `inventory.yaml`, and
reading it from the compliance stage means importing from `api/` — while
`checker.py` opens by stating it never touches an appliance. Left in place,
that sentence becomes false at the import line, and the package boundary
that DECISIONS line 11 describes stops being true of the code. See §3.

**Related open question**, listed under *Not in scope* below: `merge` raising
on every disagreement means a role template can never deliberately override
a baseline default. If that is wanted, it needs explicit override syntax
rather than last-wins, and it is cheaper to design now than to retrofit.

---

## 1. `fortigate/compliance/template.py` — NEW

Four things, in the order the pipeline uses them.

### `load_template(path) -> str` / `load_vars(path) -> Dict`

Thin loaders. `load_vars` moves from `checker.py` unchanged.

`load_template` is new and exists to split the file read out of the render:
`render_template` currently takes a `Path` and reads it, which fuses I/O to
computation and forces a fixture file into every render test. Per DECISIONS
line 3, the loader is the caller's to use and the library computes.

### `render(text, variables) -> Dict[str, Any]`

`render_template`'s body, minus the read. Keeps `StrictUndefined` and the
render-then-parse order, and the docstring explaining why both are load-
bearing — an undefined variable must not become an empty string that asserts
a value nobody wrote.

### `validate(document) -> None`

New, and the substantive part of the split. Walks the whole rendered
document and raises `TemplateError` on anything no firewall state could
satisfy.

Currently the only such check is the bool rejection inside `comparable`,
which fires **only on paths the firewall actually exported**. A bool sitting
on a path that came back UNKNOWN is never seen: `check_template` files the
path and returns without ever comparing below it. Fix the export months
later and a template bug that was always there surfaces for the first time,
looking like a regression in the export.

Validating the document up front decouples the two. It also makes DECISIONS
line 17 structural rather than incidental — a fault in the rules is caught
by the code that owns the rules, not by the code examining the subject.

`TemplateError` moves here with it.

### `merge(documents) -> Dict[str, Any]`

New. Takes rendered docs in application order, returns one.

- **Deep**, not per-cmdb-path. Two templates touching different fields of
  the same `firewall/address` object must stack, not clobber at the top.
- **Identical values are not a conflict.** A baseline and a role template
  both asserting `admintimeout: 5` is normal overlap and must pass silently.
- **Differing values at one coordinate raise `TemplateError`.** Two
  templates disagreeing is a fault in the rules, not an observation about
  the firewall (DECISIONS line 17), so it must not become a finding. The
  message names the coordinate and both values.
- **A merge of one is not a special case.** A host with a single template
  goes through the same call. No `if len(documents) == 1` anywhere
  (DECISIONS line 10).

---

## 2. `fortigate/compliance/checker.py` — REDUCED

Delete `load_vars` and `render_template`. `check_template` keeps its
signature: it already takes an in-memory `template: Mapping` and does not
care that it came from several files.

`TemplateError` leaves the module but **one raise site stays**, importing it
from `template.py`: the mapping-vs-scalar mismatch in `check_object` and
`check_section`. That one cannot move — you cannot know the template expects
a mapping where the firewall holds a scalar without the firewall's value in
hand. It is detected at comparison time even though it is a template fault.

This is the one place the no-arrow rule bends, and it bends to a single
imported exception type rather than to behaviour.

`comparable` loses its bool branch to `validate`. It keeps the mapping
branch, for the same reason.

---

## 3. `fortigate/inventory.py` — MOVED from `fortigate/api/inventory.py`

Template selection is a fact about the host, so it belongs in
`inventory.yaml` — but that file currently loads through `fortigate/api/`,
and the compliance stage must not import from `api/`. `checker.py` opens by
stating it never touches an appliance; an arrow into the API package
contradicts that for a field that has nothing to do with connecting.

The inventory is a fleet manifest read by two stages, not an API concern.
The second caller now exists, so the abstraction moves (DECISIONS line 6):

- `fortigate/inventory.py` — `FirewallEntry`, `Inventory`, unchanged logic.
- `fortigate/api/` and `fortigate/compliance/` both depend on it; it depends
  on neither.

Update imports in `fortigate/config/exporter.py` and `scripts/export_fw1.py`.

### New `FirewallEntry` field

```yaml
- name: fw1
  address: fw1.example.internal
  token: REPLACE_WITH_YOUR_API_TOKEN
  templates:
    - baseline.yaml
    - branch-office.yaml
```

`templates: List[str]`, defaulting to empty. Order is application order, and
`merge` consumes it in that order.

**Empty is not "check nothing".** A host with no `templates` has never had a
policy chosen for it, which is different from a host that passed its checks.
`Inventory.load` does not raise on it — export and normalize are perfectly
valid for such a host — but `check_fw1.py` refuses to write a diff, the same
way it refuses when the host was never normalized. A clean-looking diff for a
firewall nobody wrote a policy for is exactly the failure mode DECISIONS
line 15 is about.

Names resolve against `templates/`. Path traversal outside that directory is
rejected at load.

Document the field in `inventory.yaml.example`.

---

## 4. `scripts/check_fw1.py` — REWIRED

Becomes the composition point:

```
inventory -> entry.templates -> load+render each -> validate -> merge
          -> check_template -> write_diff
```

Validation runs per rendered document, before the merge, so an error names
the file it came from.

Exit codes are unchanged: 1 on FAIL or MISSING, 0 on UNKNOWN alone. A
`TemplateError` from any stage aborts before a diff is written — a template
fault must not leave behind a file that reads as a statement about the
firewall.

New failure mode to report distinctly: host absent from inventory, and host
present with no templates.

---

## Not in scope

- Fleet-wide checking (`check_all.py`). Still one showcase script per stage.
- Template inheritance or conditionals beyond what Jinja2 already gives.
  `merge` composes whole documents; it does not introduce a layering
  language.
- Moving `template.py` to package level. It stays under `compliance/` while
  compliance is its only consumer; it moves the day something that is not a
  checker renders one (DECISIONS line 6).
