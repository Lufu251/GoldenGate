# Open points

Everything the three implemented stages left unfinished, in one place. Carried
out of `plans/compliance-checker.md` when that plan was retired: stages 1--3 are
built and described in `README.md`, so what remained of it was this.

Nothing here is committed to. Each item records the reasoning that produced it
so whoever picks it up does not re-derive it, and several are deliberately
*not* built yet under DECISIONS line 6 (build an abstraction when a caller
exists).

`plans/template-renderer.md` is the one plan already written against this list;
it owns template rendering, validation, merging, and per-host selection. Items
below that touch it say so rather than restating it.

---

## 1. Default suppression -- the big one

`system/global` normalizes to **238 keys** (verified against the current
`data/normal/fw1.yaml`), of which maybe ten were ever set by an operator. The
structure problem is solved; the volume problem is not, and it is where the real
readability win lives.

Three ways to do it:

- **Subtract a factory-default export** -- accurate, needs no schema, but needs
  one baseline export per FortiOS version and a spare appliance.
- **Subtract the API schema** -- `?action=schema` returns each attribute's
  `default`. Self-describing and per-version-correct, at the cost of one extra
  API call per path at export time. **Preferred.**
- **Keep everything** -- current behaviour.

Do it as a **separate pass over `data/normal/<host>.yaml`** rather than folding
it into the normalizer, so it can be developed and tested independently
(DECISIONS line 8).

This is the highest-leverage item on the list because two others collapse into
it: it largely answers template versioning (§6) and it makes config generation
correct by construction (§2).

## 2. Generating standard FortiGate config

Two distinct outputs, both rendering from `data/normal/<host>.yaml`:

- **CLI text** -- `config firewall address` / `edit "x"` / `set ...` / `next` /
  `end`. The mkey-keyed mapping shape maps 1:1 onto `edit <mkey>` blocks.
- **API payloads**, for pushing config back.

**Key insight for whoever builds this:** FortiGate CLI is itself
default-suppressed -- `show` omits defaults, `show full-configuration` does not.
So once §1 lands, the normalized YAML *is* `show`, and CLI generation becomes
correct by construction. One transform serves both diffing and generation, which
is the second reason to do defaults first.

**Open question:** is generated config meant to be round-trippable (export → YAML
→ CLI → apply, same box back), or just human-readable for review? Round-trip
raises the stakes on ordering considerably. Answer this before writing code.

Both are renderers over the same normalized data, so a third output format is an
addition rather than a rewrite (DECISIONS line 13).

## 3. Fleet script

Still one showcase script per stage, each naming its one host. The fleet loop is
the obvious next runnable, and its policy is **already settled** -- it only needs
writing:

- All hosts are checked and summarized. Per-host errors are caught per iteration
  rather than aborting, so one failure cannot hide the remaining hosts.
- A host that raises (never normalized) is neither compliant nor
  non-compliant -- the same trichotomy as FAIL/MISSING/UNKNOWN, one level up.
- **Errored hosts do not fail the run; zero successfully-checked hosts does.**
  If `normal_dir` is misconfigured or empty, every host raises, nothing is
  checked, and the summary would otherwise report a green run that verified
  nothing. Unlike UNKNOWN there is no upstream red run to catch that, because a
  bad `normal_dir` never touches stage 1 or 2. "I checked nothing" is a distinct
  failure from "I checked things and one was unchecked".

Matching `export_fw1.py`'s any-failure-is-1 was **rejected**: the exporter talks
to appliances, where a failure means something broke *now*, while a fleet check
reading disk will routinely find hosts mid-onboarding. Making that red trains
people to ignore red.

This policy lives in the fleet script only; the library has no notion of it.

The **host-to-template mapping** the fleet loop needs is `plans/template-renderer.md`'s
`templates:` field on the inventory entry. It was deliberately not built while
each script names its one host, because it would have had no caller.

## 4. Template composition and selection

Owned by `plans/template-renderer.md` (`merge`, `validate`, the inventory
`templates:` field). The checker takes **one rendered template and one
normalized host** and needs no change for any of it -- the rendered template is
already a parameter.

What that plan does not record, and should not be lost: heterogeneity is **two
axes**, and solving both with one mechanism is the trap to avoid.

- **Model capability** -- *can* this box have this path? A path unsupported on a
  model fails to fetch, is recorded in `FailedSection`, is never written, and is
  therefore absent from the normalized file -- which is exactly UNKNOWN. Model
  differences already degrade to "could not check" rather than "firewall is
  broken", with no new construct. **No overlay can fix a box that lacks a
  feature: there is no correct `expected:` value.**
- **Role / use-case** (branch vs DC) -- what *should* this box's values be? A
  composition problem, solved by merging templates into one document before the
  checker sees it.

The cost to watch: on a small model checked against a large baseline, the
standing block of UNKNOWNs never resolves and UNKNOWN stops reading as a signal.
That is a reporting concern for fleet tooling (§3), not a checker change.

## 5. Cross-VDOM rules

Two things `check_template` deliberately does not do. Both need new template
syntax *and* a new result type, so neither is a small addition:

- **Consistency assertions** -- "every VDOM must have the *same* value, whatever
  it is". Relational rather than desired-state, with no `expected:` to put in the
  diff.
- **Per-VDOM exceptions** -- "this value, but only in `dmz`". This is the overlay
  problem, and belongs to template composition (§4) rather than to the checker,
  which consumes an already-composed template either way. fw1 now has two VDOMs,
  so it is closer than it was.

## 6. Template versioning by FortiOS release

Open, and **largely dissolves if §1 lands**, since defaults are the main thing
that differs between versions. Revisit only after default suppression, not
before.

## 7. A distinct exit code for UNKNOWN-only

`check_fw1.py` exits 0 on UNKNOWN alone, on the argument that stage 1 already
failed loudly and named the VDOM, path, and API error. If CI ever needs to tell
"compliant" from "compliant but incompletely checked", **exit 2 for UNKNOWN-only
is the upgrade** -- not making UNKNOWN red.

## 8. Where type coercion lives

`comparable` in `checker.py` is a second place that knows the API lies about
types, which sits slightly against DECISIONS line 9 (canonical form before
comparison). It stays there because the normalizer cannot fix it without data
loss: there is no coercion that repairs `'3'` and preserves `'000000'`.

It is kept in one named function on purpose. **If config generation (§2) ever
needs the same coercion, that is the signal to move it down into the
normalizer** -- a second caller is the trigger, per DECISIONS line 6.

## 9. No tests

There is no test suite at all. Every claim in the design docs was settled against
real data in `data/`, which is gitignored and comes from one lab appliance -- so
the evidence for the current behaviour is not reproducible by anyone else and
not checked by anything.

The pipeline was built to make this cheap: each stage reads a fixed on-disk input
and writes a deterministic output, so fixtures are just small files. The
normalizer and checker are pure and need no appliance.

Worth doing before the fleet script (§3), which multiplies the blast radius of
any regression. `plans/template-renderer.md` also notes that `render_template`
currently fuses the file read to the render, forcing a fixture file into every
render test -- splitting that out is a prerequisite.

---

## Corrections found while checking the old plan against the code

Recorded because the retired plan asserted them and the code disagrees. The code
and its docstrings are the accurate record in all three cases.

- **`check_section` does dispatch on kind**, contrary to the old plan's "there is
  deliberately no table-vs-singleton dispatch". It branches on whether the
  *template's* value is a mapping, to decide whether a depth-one key names an
  object or a field of the section itself. The headline claim still holds in
  spirit -- it never sniffs the *firewall's* value types, and both branches
  render to the same place, so a wrong guess costs nothing structural. The
  docstring in `checker.py` states this precisely; the plan oversimplified it.
- **The raw export layout is nested, not flattened.** The old plan wrote
  `data/raw/<host>/<scope>/cmdb-*.json` in two places; the actual layout is one
  directory per cmdb path segment, `data/raw/<host>/<scope>/cmdb/<path>.json`.
  Flattening was rejected as irreversible (`system/dns-database` would read back
  as `system/dns/database`).
- **`exporter.py:213` does not exist** -- the file is 155 lines. The claim it
  supported is still true: `fetch_section` records failures per `(vdom, path)`,
  which is why UNKNOWN is reported per VDOM rather than hoisted.
