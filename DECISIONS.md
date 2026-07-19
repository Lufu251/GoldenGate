# Architectural Decisions

- **Caller-driven configuration over hardcoded lists**: what to operate on is passed in by the caller; libraries never read configuration files themselves, though dedicated loader modules may exist for callers to use.
- **One unit of work per call**: library functions operate on a single subject and return its result; iterating over many is the caller's job.
- **Build an abstraction when a caller exists**: indirection is added at the point something needs it, not in anticipation of a future need.
- **Auto-discovery over static assumptions**: state is discovered from the system or artifact being operated on, rather than assumed, hardcoded, or re-read from the declaration that produced it.
- **Persist between pipeline stages**: each stage of a transformation pipeline writes its output to disk, so downstream stages can be built and tested against fixed inputs without re-running upstream ones.
- **Canonical form before comparison**: data is normalized into a stable, noise-free shape once, so every consumer compares and renders from the same representation instead of each re-implementing the cleanup.
- **Packages grouped by responsibility, with one-way dependencies**: code is partitioned into packages by the concern it serves, and the dependency arrows between them run in a single direction only.
- **Small single-purpose functions over one large function**: each step is isolated and independently returns a concrete value; orchestration is kept separate from logic.
- **Compute a structured result, format it separately**: rendering is a pass over a result object, so an alternative output format is an additional renderer rather than a rewrite.
- **Results carry the identity of their subject**: a result object names what it describes, so callers can aggregate or report without threading context alongside it.
- **Absence of data is distinct from failure**: "not checked" and "checked and wrong" are separate outcomes, never collapsed into one.
- **Fail loudly rather than substitute silently**: undefined inputs and lossy conversions raise, instead of defaulting to a value that yields a plausible wrong answer.
- **Distinct outcomes are distinct types**: a result variant carries exactly the fields it can have, rather than one type with a status flag and optional fields, so a state the format cannot express cannot be constructed either.
- **Encoder and decoder live together**: whatever names a document's keys as literals owns reading them back, so no consumer holds a second copy of the schema that can drift from it.
- **Secrets and generated data are excluded from version control** by policy, not just convention.
