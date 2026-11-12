---
name: reuse-discovery
description: Understand what a user wants to build, identify the kinds of reusable artifacts they need, inspect available providers, and route discovery to the appropriate provider-specific skill and tools. Use before building a substantial capability or adding an external project, model, or dataset.
---

# Reuse discovery

This is the provider-neutral entry point for finding existing projects, models, datasets, and other
reusable artifacts. Understand the user's need first; use provider-specific workflows for searching and
vetting. Do not assume GitHub is the only useful source.

## 1. Understand the need

Translate the request into:

- **Job to be done:** one sentence describing the desired outcome, not a bag of keywords.
- **Artifact types:** one or more of `repositories`, `libraries`, `applications`, `developer-tools`,
  `models`, `datasets`, `spaces`, `notebooks`, or another type the available providers declare.
- **Components:** independently useful parts of the solution. Give each a short name and its own
  requirements. Do not split one capability into artificial tiny components.
- **Must-have capabilities:** observable functions or properties an artifact needs to provide.
- **Constraints:** language/runtime, deployment, license, data/model format, freshness, privacy, and
  exclusions. Distinguish requirements from preferences.
- **Ambiguities:** identify alternative meanings of important terms and exclude the unintended ones.

For example, "a speech recognition feature with a way to evaluate accuracy" may need a model and an
evaluation dataset; it is not necessarily a request for a complete application.

## 2. Inspect available providers

Call the MCP tool `list_providers` before routing. Treat its response as the source of truth for provider
availability, artifact types, strengths, installed skills, and tools.

- Use only providers whose status is `available`.
- A provider marked `planned` is not searchable yet. Say so if relevant; do not imply that it was checked.
- Providers can overlap. Select every available source that can materially help with the requested
  artifact types; do not force each type to exactly one provider.
- If no available provider fits, explain the gap rather than pretending a search was run.

## 3. Route each component

For each component, select provider-specific workflows based on the registry. Apply the corresponding
provider skill and use only tools declared for an available provider. Follow that provider's search
limits, evidence rules, security checks, and retry guidance.

Keep checks focused and sequential: finish a component's provider checks before starting the next. If a
provider result is inconclusive, retry only that component using the provider workflow. An inconclusive
result is missing evidence, not evidence that no suitable artifact exists.

Search more than one provider when the need spans artifact types or the registry indicates meaningful
overlap. Do not silently substitute one artifact type for another (for example, a code repository for a
hosted model).

## 4. Compare results using the shared record

When comparing providers, ask each provider tool for its `normalized` record for each returned artifact
(for GitHub, pass `include_normalized: true`). Use that record as the cross-provider comparison surface:

- `schema_version`: version of the shared record contract.
- `provider_id`, `artifact_type`: source and kind of artifact.
- `id`, `name`, `url`: stable source identity and human-readable location.
- `version`, `revision`: release/model/data version and immutable source revision when available;
  otherwise `null`.
- `license`: SPDX identifier or provider-reported license when known; otherwise `null`.
- `evidence`: must-have capabilities and the source of evidence, plus completed checks.
- `limitations`: unresolved checks, missing evidence, and integration risks.

The GitHub workflow also returns its established fields (`verdict`, `picks`, `coverage`, `gates`, and
others). Preserve and consult those source-specific details. A normalized record makes fields easier to
compare; it does not make different providers' scores or checks equivalent. Never compare provider scores
as if they were calibrated to the same scale.

When presenting results:

- Group by component, then provider and artifact.
- Compare capability evidence, license, version/revision, provenance, and limitations.
- Say explicitly when a field is unknown or a provider was unavailable.
- Never say an artifact is safe or fully compatible solely because it passed automated checks.
- Recommend only artifacts actually returned by provider tools; keep exact versions/revisions where given.
- Do not install, download, or integrate anything without user approval.

The parent skill owns intent and routing. Each provider skill owns its own search and source-specific
vetting. Add providers by registering their artifact types, strengths, skill, and tools, then implementing
the provider adapter and the same normalized result contract.
