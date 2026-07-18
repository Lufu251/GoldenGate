# Architectural Decisions

- **Caller-driven configuration over hardcoded lists**: what to operate on is passed in by the caller, not baked into the library or read from a separate config file.
- **Auto-discovery over static assumptions**: topology is discovered from the live system at call time rather than assumed or hardcoded.
- **Persist between pipeline stages**: each stage of a transformation pipeline writes its output to disk, so downstream stages can be built and tested against fixed inputs without re-running upstream ones.
- **Canonical form before comparison**: data is normalized into a stable, noise-free shape once, so every consumer compares and renders from the same representation instead of each re-implementing the cleanup.
- **Small single-purpose functions over one large function**: each step is isolated and independently returns a concrete value; orchestration is kept separate from logic.
- **Secrets and generated data are excluded from version control** by policy, not just convention.
