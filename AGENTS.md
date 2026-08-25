# Hermes Agent — Contributor Instructions

This file contains only durable, cross-repository invariants. Discover implementation details from the source and current documentation; do not preserve inventories, line counts, constructor snapshots, or feature manuals here.

## Prove the problem before changing code

- Reproduce or trace the reported behavior from its real entry point through state, I/O, and user-visible output.
- Recover intent from callers, tests, neighboring code, and relevant history. A failing test or issue description is evidence, not automatically the contract.
- Fix the bug class, including equivalent sibling paths, rather than patching one observed call site.
- Keep one change focused on one useful outcome. Do not mix the fix with unrelated cleanup or speculative hardening.

## Keep the core narrow

Use the smallest footprint that delivers the capability:

1. improve an existing path;
2. expose it through CLI plus a skill;
3. add a gated core tool only when model-native tool use is required;
4. use a plugin or MCP server for optional, provider-specific, or third-party behavior;
5. add an always-loaded core tool only when the capability is broadly useful, stable, and cannot work without built-in bootstrap.

Do not add speculative services, telemetry, or always-on background work. New network access, external writes, or data collection must be explicit and opt-in.

Third-party product integrations belong in standalone plugins, skills, or MCP servers unless Hermes itself owns the generic interface. Core plugin managers and registries must stay product-agnostic.

## Preserve prompt and schema stability

- Treat the system prompt, tool schemas, skill index, and other stable prefixes as immutable after the first model turn whenever possible. Byte changes invalidate prompt-cache reuse.
- Put timestamps, runtime state, fetched data, and per-turn hints after the stable prefix or in the user/ephemeral turn.
- Evolve public schemas and callback payloads additively. New fields and parameters must be optional; do not rename or remove existing ones without an explicit migration.
- Do not mutate process-wide environment variables or registries to represent session-specific capability or state.

## Configuration, profiles, and secrets

- Store non-secret settings in `config.yaml` and load them through the canonical config helpers. Environment variables are compatibility fallbacks, not the primary configuration API.
- Store credentials only in the configured secret source or profile `.env`; never place them in YAML, logs, fixtures, or chat output.
- Resolve state through `get_hermes_home()` or an explicit profile home. Never hardcode `~/.hermes`; the default profile and named profiles have different physical layouts.
- Keep display paths separate from state-resolution paths. User-facing output may show the logical profile path even when storage uses a compatibility location.
- Anchor profile roster discovery to the launch home, not a mutable child-profile home.
- In multiplexed processes, credential and authorization lookup must fail closed for the requested profile. Never fall back to another profile's secret, adapter, cache, or lock.
- Key mutable caches, locks, and singletons by the full authority scope they protect: at minimum profile, and where applicable platform, account, or session.

## Surface and process boundaries

- A UI or transport capability is a session property, not a backend-process property. Pass capabilities explicitly through the session/request path; do not gate them with a shared process environment variable.
- Keep adapters responsible for transport and rendering. The owning consumer declares completion and semantics; adapters must not guess whether a stream frame is final.
- Progressive streaming must be monotonic and prefix-based, and final delivery must be idempotent. A duplicate final frame must not create duplicate user-visible output.
- Authorize actions against the profile and adapter that own the session. A default-profile fallback is a security bug.
- When supervising wrapped processes, identify and manage the real worker process or process group, not only the launcher PID. Recovery must not leave orphan workers.
- Bot identity follows the registered Bot/profile name. Transport chat IDs and canonical-chat overrides route messages; they do not redefine identity.

## Extension compatibility

- Keep plugin hooks, `PluginContext`, callback signatures, and registry contracts backward-compatible. Filter payloads for narrow callbacks; callbacks accepting `**kwargs` may receive the complete additive payload.
- Generic plugin code must not import optional product implementations. Official integrations must obey the same isolation boundary as third-party integrations.
- An optional integration may fail or be absent without breaking core startup, configuration, help output, or unrelated tools.
- Before changing a tool, plugin, provider, skill, or model surface, inspect its discovery path, schema generation, configuration flow, and all consumers. Do not update only the obvious registry.

## Dependencies

- Bound every runtime dependency with a justified upper limit.
- Pin Git dependencies to immutable commit SHAs; never ship a moving branch or tag.
- Update the requirement, lock data, and compatibility tests together.
- Avoid new dependencies when the standard library or an existing dependency is sufficient.

## Testing and proof

- Run tests through `scripts/run_tests.sh`; use the smallest targeted suite that proves the change, then the relevant integration or platform lane when the risk requires it.
- Tests that touch configuration, credentials, plugins, caches, or filesystem state must use an isolated temporary `HOME`/Hermes home. Never read or write the developer's real profile.
- Keep unit tests offline and deterministic. Patch network and subprocess boundaries unless the test is explicitly an integration or E2E lane.
- Test behavior and relationships: invariants, round trips, contracts, and user-visible outcomes. Do not freeze implementation names, exact inventories, historical counts, or incidental formatting.
- Do not read `.py`, `.ts`, `.tsx`, or other source text in tests to assert that code contains a call or pattern. Extract the logic and execute it. Manifest, packaging, and generated-artifact tests may inspect the artifacts they own.
- A mocked foreign operating system cannot prove OS integration. Unit-test pure decision logic with injected platform values, but run process, signal, console, filesystem, and installer behavior on the real target OS.
- Every bug fix needs a regression test that fails for the original defect and passes for the corrected behavior unless the test would be less reliable than the production contract it claims to prove.
- Finish with `git diff --check` and inspect the actual diff. Preserve unrelated user changes.

## High-risk subsystem invariants

- **Updates:** keep prepare, execution, activation, and receipt explicit. Interrupted updates must be recoverable; fleet-wide activation must account for every affected gateway/profile.
- **Gateway callbacks:** hold synchronization for the entire non-thread-safe critical section, including lazy initialization and state transitions.
- **Tools:** tool handlers return the registry's JSON-string contract. Avoid direct imports between unrelated tools; extract shared implementation into a neutral module.
- **Cron:** script-only jobs deliver exact stdout; empty output is silent and non-zero exit is an alert. Agent-driven jobs must not silently degrade into script semantics.
- **Persistence:** distinguish active model context, stored transcript, cumulative usage, and cache accounting. Optimizing one does not imply the others changed.

## Scoped guidance

Read the narrowest relevant source before editing:

- Desktop architecture and UX: `apps/desktop/AGENTS.md`
- System architecture: `website/docs/developer-guide/architecture.md`
- Plugin development: `website/docs/developer-guide/plugins/index.md`
- Skill authoring: `website/docs/developer-guide/creating-skills.md`
- User configuration behavior: `website/docs/user-guide/configuration.md`

If behavior changes, update the owning documentation in the same change. Add detail to a scoped guide or test contract rather than expanding this root file.
