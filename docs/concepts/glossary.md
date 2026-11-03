# Glossary

Sidegraph keeps the project's decision graph for AI coding agents. This page defines the terms used by the guides and reference.

| Term | Meaning |
|---|---|
| Record | A stored decision, fact or domain. A decision includes the reason, choice and rejected alternatives. A fact holds non-derivable knowledge and its source. |
| Anchor | A reference connecting a record to the code, document or concept it describes. Pair a precise symbol or heading with its containing file. |
| Entity | An identity for an anchor target. Concrete targets and named abstract targets persist in the store. Derived community entities live only in the index and can be recreated. A descriptor, when present, names the target and optionally its repository-relative file. |
| Binding | The link between a record and an entity. It carries a tier, weight, relation and current resolution status. |
| Tier 2 / leaf | An anchor to a concrete code or document entity, such as a function or heading. |
| Tier 1 / group | An anchor to a named domain or a graph community. It keeps area-level context reachable. |
| Tier 0 / abstract | An anchor to a semantic grouping, such as a tag or initiative. |
| Domain | A human-named area of the project with a title, purpose and membership rules. Accepted domains form the session's table of contents. |
| Community | A cluster of related graph nodes computed by the engine. Its numeric ID can change after rebuilding. It is not a durable project name. |
| God node | The engine's representative or hub node for a community. Before domains are named, this may label an area in the session map. |
| Ratify | Review a proposal and accept or reject it. With no configured policy, proposals wait for a human. Setup offers an opt-in policy that can accept eligible proposals and stamp that provenance. |
| Proposed / accepted | Proposed means awaiting a trust decision. Accepted means ratified. A visible proposal is marked unratified, so visibility does not imply acceptance. |
| Supersede | Replace a record with a successor while retaining the predecessor and its history. Use this when a decision or fact changes. |
| Drift | A signal that anchored code changed or a previous verification no longer applies. It asks for a content check and does not prove the record false. |
| Live / degraded / orphaned | Binding states. Live resolves at its tier. Degraded means resolution is ambiguous. Orphaned means its target cannot be resolved. A surviving broader anchor can still make the record reachable. |
| Seed | A file or entity supplied to retrieval to identify the current task's neighborhood. It is an input to the lookup. |
| Store / index | The store is the authoritative committed record data. The SQLite index is derived local state and can be rebuilt. Commit the store and ignore the index. |
| Format 1 / format 2 | Store layouts. Format 1 uses per-record files and legacy archives. Format 2 also supports hash-keyed shard files. New records still start as individual files. |
| Shard | A canonical file of typed JSON lines used to pack settled record bodies in format 2. It is authoritative store data, unlike the index. |
| Session map / TOC | The initial orientation delivered to an agent. Named accepted domains form its table of contents. Hook-delivered maps use a graph-based fallback when no accepted domains exist. Grok MCP instructions then provide only the standing instruction and version. OpenCode has no opening map. |
| Hook | A host event handler that can deliver context or a capture reminder. Which events reach the agent depends on the host. |
| Capture | Writing durable decisions or facts from work, including proposals awaiting review. |
| Descriptor | The name and optional file path used to resolve an entity against the current graph. |
| Initiative | An abstract label grouping decisions about a piece of work. It has no independent domain-style record. |

For record fields, see [data model](data-model.md). For resolution rules, see [anchoring](anchoring.md). For files on disk, see [store format](../reference/store-format.md).
