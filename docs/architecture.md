# Reuse Layer architecture

Reuse Layer helps an agent find and assess reusable open-source components before building them. The
architecture separates provider-neutral intent and routing from provider-specific discovery and checks.

## System view

```mermaid
flowchart LR
    User[User request] --> Agent[Agent]
    Agent --> Parent[Parent skill<br/>intent and artifact types]
    Agent --> MCP[MCP server<br/>list_providers, GitHub tools]
    MCP --> Registry[list_providers tool]
    Registry --> Catalog[Packaged provider registry]
    Parent --> GitSkill[GitHub discovery skill]
    GitSkill --> Agent
    Shell[CLI] --> CLIAdapter[CLI adapter]
    MCP --> Engine[Reuse Layer engine]
    CLIAdapter --> Engine
    Engine --> Adapter[GitHub source adapter]
    Adapter --> GH[GitHub REST API]
    Engine <--> Cache[(SQLite HTTP cache<br/>and outcome ledger)]
    Config[User config<br/>keys and policy] --> Engine
    Engine --> Checks[Metadata gates<br/>evidence enrichment<br/>capability matching]
    Checks --> Relevance[Lexical relevance<br/>optional model judge]
    Relevance --> Rank[Coverage-first ranking<br/>and component verdict]
    Rank --> Result[GitHub result<br/>optional normalized records]
    Result --> MCP
    MCP --> Agent
    MCP --> Feedback[report_outcome]
    Feedback --> Engine

    Parent -. planned provider skill and adapter .-> HF[Hugging Face]
    Parent -. planned provider skill and adapter .-> Kaggle[Kaggle]
```

GitHub is the only active discovery provider today. Hugging Face and Kaggle are catalog entries marked
`planned`; the dashed connections are an extension point, not working integrations. The registry is
packaged at `src/reuse_layer/integrations/providers.toml` and surfaced to the parent through
`list_providers`. It describes capabilities but does not execute searches.

## Request flow

```mermaid
flowchart TD
    Start[User describes a goal] --> Intent[Parent skill identifies job, constraints, ambiguities]
    Intent --> Types[Identify artifact types and independent components]
    Types --> Registry[Read available providers and strengths]
    Registry --> Route{Available provider fits?}
    Route -- No --> Gap[Explain source coverage gap]
    Route -- Yes --> ProviderSkill[Apply provider-specific skill]
    ProviderSkill --> Search[Search source and gather candidates]
    Search --> Vet[Verify identity, evidence, license, version and source checks]
    Vet --> Normalize[Emit source details and normalized artifact records]
    Normalize --> Compare[Compare evidence and limitations by component]
    Compare --> Decision[Present recommendations, gaps and unresolved checks]
    Decision --> Approval{User approves integration?}
    Approval -- No --> Done[Stop; nothing installed]
    Approval -- Yes --> Integrate[Integrate exact returned revision]
    Integrate --> Outcome[Report integration outcome when supported]
```

Each component receives its own provider check and decision. Run checks sequentially to keep each request
focused and make timeout retries easier to interpret. A provider-specific result remains authoritative
for its own verdict and checks; normalized records provide shared fields for comparison, not a shared
score or an assertion that checks are equivalent.

## Shared normalized artifact record

Every provider adapter should expose this additive record alongside its provider-specific result. GitHub
includes it when `find_reusable` is called with `include_normalized=true`, keeping the default response
compact:

| Field | Meaning |
| --- | --- |
| `schema_version` | Version of the normalized record contract. |
| `provider_id`, `artifact_type` | Provider registry ID and artifact category. |
| `id`, `name`, `url` | Stable provider identity, label, and canonical location. |
| `version`, `revision` | Human-facing version and immutable revision, when available. |
| `license` | SPDX or provider-reported license; `null` when not established. |
| `evidence` | Capability matches, evidence locations, and checks actually completed. |
| `limitations` | Missing evidence, unresolved checks, and integration risks. |

Unknown values stay unknown; adapters must not fill gaps with guesses. Keep provider scores, verdicts,
licenses, and security findings in their original context because providers may use different
methodologies.
