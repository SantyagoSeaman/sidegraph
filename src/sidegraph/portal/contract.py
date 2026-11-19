"""Portal HTTP routes and wire models.

See design/superpowers/specs/2026-11-17-portal-design.md (D3–D13).
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Domain, DomainStatus, Fact

THEME_MARKER = "__THEME_CLASS__"
VIS_NETWORK_PATH = "/static/vendor/vis-network.min.js"

type Theme = Literal["dark", "light"]
type HostFamily = Literal["claude", "codex", "grok", "muse"]
type RecordType = Literal["decision", "fact", "domain"]
type RecordStatus = DecisionStatus | DomainStatus
type Layer = Literal["business", "technical"]
type AnchorWord = Literal["found", "ambiguous", "missing", "unchecked"]
type BindingStatus = Literal["live", "degraded", "orphaned"]
type AnchorTier = Literal[0, 1, 2]
type Change = Literal["records", "graph", "advice", "sync"]
type HookEvent = Literal[
    "session_start", "pre_tool_use", "agent_brief", "subagent_start", "stop", "mcp_instructions"
]


class Settings(BaseModel):
    """Effective settings after defaults and clamping (D12)."""

    theme: Theme
    poll_seconds: int = Field(ge=2, le=60)
    editor_url: str
    idle_minutes: float = Field(ge=0.05)


class Session(BaseModel):
    """Launch, store and graph state for the shell and queue (D1, D5, D9, D13)."""

    host_family: HostFamily | None = None
    mode: Literal["cli", "chat"]
    settings: Settings
    repo_name: str | None = None
    branch: str | None = None
    index_cold: bool
    graph_newer_than_sync: bool
    store_changes: int | None = None
    started_at: AwareDatetime
    reviewing_until: AwareDatetime | None = None


class LiveTicket(BaseModel):
    """Short-lived, single-use credential for EventSource (D3)."""

    ticket: str


class _ChangedData(TypedDict):
    what: Change


class _ChangedEvent(BaseModel):
    event: Literal["changed"]
    data: _ChangedData


class _StatusEvent(BaseModel):
    event: Literal["stale", "closing"]
    data: Literal[""] = ""


type LiveEvent = Annotated[_ChangedEvent | _StatusEvent, Field(discriminator="event")]


class _ColorPair(TypedDict):
    background: str
    border: str


class _NodeColor(_ColorPair):
    highlight: _ColorPair


class _GraphNode(TypedDict):
    id: str
    type: Literal["decision", "fact", "entity"]
    label: str
    title: str
    color: _NodeColor
    shape: Literal["box", "diamond", "dot"]
    size: float
    opacity: float
    problem: bool
    dangling: bool
    detail: dict[str, JsonValue]


class _EdgeColor(TypedDict):
    color: str
    opacity: float


_GraphEdge = TypedDict(
    "_GraphEdge",
    {
        "from": str,
        "to": str,
        "kind": Literal["anchor", "supersedes", "supports", "domain-parent"],
        "color": _EdgeColor,
        "width": float,
        "label": str,
        "dashes": bool | list[int],
        "arrows": Literal["to"],
        "status": BindingStatus | None,
        "relation": str | None,
        "weight": float,
    },
)


class _GraphStats(TypedDict):
    decisions: int
    facts: int
    entities: int
    orphaned_bindings: int
    degraded_bindings: int
    dangling_records: int
    supersede_links: int
    truncated: int


class Graph(BaseModel):
    """The viz JSON with per-record domain membership (D4)."""

    nodes: list[_GraphNode]
    edges: list[_GraphEdge]
    stats: _GraphStats
    record_domains: dict[str, list[str]]
    membership_certified: bool


class RecordRow(BaseModel):
    """Summary used in the record table and version chain (D4–D5)."""

    id: str
    type: RecordType
    kind: DecisionKind | None = None
    status: RecordStatus
    domains: list[str]
    title: str
    layer: Layer | None = None
    anchor_tiers: list[AnchorTier]
    anchor_statuses: list[AnchorWord]


class Records(BaseModel):
    """The filtered records table (D4)."""

    rows: list[RecordRow]


class _Match(TypedDict):
    words: list[str]
    via_anchor: str | None


class _SearchHit(BaseModel):
    id: str
    type: RecordType
    kind: DecisionKind | None = None
    status: RecordStatus
    domains: list[str]
    title: str
    match: _Match
    snippet: str


class SearchResult(BaseModel):
    """Ordered matches and a unique record-id prefix hit, when present (D4)."""

    hits: list[_SearchHit]
    open_id: str | None = None


class Anchor(BaseModel):
    """One binding's display status and optional code span (D5)."""

    entity_id: str
    name: str
    tier: AnchorTier
    file: str | None = None
    start: int | None = None
    end: int | None = None
    status_word: AnchorWord
    internal_status: BindingStatus
    community: str | None = None
    editor_path: str | None = None


# Wire records are existing records. The creation defaults in the store schema would
# silently mint an identity or turn a response with no status into a proposal.
class _DecisionRecord(Decision):
    model_config = ConfigDict(from_attributes=True)
    id: str = Field()
    status: DecisionStatus = Field()


class _FactRecord(Fact):
    model_config = ConfigDict(from_attributes=True)
    id: str = Field()
    status: DecisionStatus = Field()


class _DomainRecord(Domain):
    model_config = ConfigDict(from_attributes=True)
    domain_id: str = Field()
    status: DomainStatus = Field()


class RecordCard(BaseModel):
    """Full record, history, evidence and conflict signals (D5, D7)."""

    record: _DecisionRecord | _FactRecord | _DomainRecord
    history: list[RecordRow]
    evidence: list[_FactRecord]
    keepable_evidence: list[str]
    drifted: bool
    pending_successor: str | None = None
    pending_evidence: list[str]
    anchors: list[Anchor]


class Snippet(BaseModel):
    """Contiguous source lines with 1-based inclusive endpoints (D5)."""

    file: str
    start: int
    end: int
    lines: list[str]
    shifted: bool


class _Advice(TypedDict):
    verdict: Literal["accept", "reject", "edit"]
    reason: str


class _Signals(TypedDict):
    anchors: list[Anchor]
    similar: list[RecordRow]


class _QueueItem[Record: (_DecisionRecord, _FactRecord, _DomainRecord)](BaseModel):
    record: Record
    advice: _Advice | None = None
    signals: _Signals

    @field_validator("record")
    @classmethod
    def _proposed_record(cls, record: Record) -> Record:
        if record.status != "proposed":
            raise ValueError("queue records must be proposed")
        return record


class _QueueDecision(_QueueItem[_DecisionRecord]):
    evidence: list[_FactRecord]

    @field_validator("evidence")
    @classmethod
    def _proposed_evidence(cls, evidence: list[_FactRecord]) -> list[_FactRecord]:
        if any(fact.status != "proposed" for fact in evidence):
            raise ValueError("queue evidence must be proposed")
        return evidence


class Queue(BaseModel):
    """Pending records and their advice; nested evidence follows its decision (D6, D9)."""

    decisions: list[_QueueDecision]
    facts: list[_QueueItem[_FactRecord]]
    domains: list[_QueueItem[_DomainRecord]]


ERROR_CODES: dict[str, int] = {
    "unauthorized": 401,
    "bad_host": 421,
    "bad_origin": 403,
    "not_found": 404,
    "not_current": 409,
    "pending_successor": 409,
    "pending_proposal": 409,
    "pending_evidence": 409,
    "evidence_conflict": 409,
    "operation_in_progress": 409,
    "op_key_reused": 422,
    "invalid": 422,
    "busy": 503,
    "refused": 409,
}


class _Error(BaseModel):
    code: str
    message: str
    details: dict[str, JsonValue]

    @field_validator("code")
    @classmethod
    def _check_code(cls, code: str) -> str:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown Portal error code: {code}")
        return code

    @model_validator(mode="after")
    def _check_refusal_reason(self) -> "_Error":
        reason = self.details.get("reason")
        if self.code == "refused" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("a refusal requires details.reason")
        return self


class ErrorBody(BaseModel):
    """JSON error envelope for D3 and D6–D8.

    ERROR_CODES maps application errors to HTTP statuses. An unsupported method
    uses the invalid code with the router's HTTP 405 and Allow header.
    """

    error: _Error


class _PreviewOutcome(BaseModel):
    outcome: Literal["accepted", "rejected", "dropped", "stays"]
    evidence_of: list[str] | None = None


class PreviewResult(BaseModel):
    """Predicted outcomes keyed by record id, including the evidence cascade (D6)."""

    outcomes: dict[str, _PreviewOutcome]


class _ApplyOutcome(BaseModel):
    outcome: Literal[
        "accepted",
        "rejected",
        "dropped",
        "already_accepted",
        "already_rejected",
        "already_dropped",
        "refused",
        "busy",
        "stays",
    ]
    evidence_of: list[str] | None = None
    error: _Error | None = None

    @model_validator(mode="after")
    def _check_refusal(self) -> "_ApplyOutcome":
        if self.outcome == "refused" and (self.error is None or self.error.code != "refused"):
            raise ValueError("a refused outcome requires an error with a reason")
        return self


class ApplyResult(BaseModel):
    """Observed outcomes keyed by record id, including when only a prefix landed (D6)."""

    outcomes: dict[str, _ApplyOutcome]


class OperationPart(BaseModel):
    """Outcome of one resumable write step (D7–D8)."""

    part: (
        Literal["drop_draft", "version", "close_predecessor", "bindings", "cascade"]
        | Annotated[str, StringConstraints(pattern=r"^fact:[^:]+$")]
    )
    outcome: Literal["done", "done_now", "failed", "skipped"]
    id: str | None = None
    message: str | None = None


class OperationResult(BaseModel):
    """Current state and step outcomes for the client's operation key (D7–D8)."""

    op_key: str
    state: Literal["planned", "started", "done"]
    parts: list[OperationPart]
    new_record_id: str | None = None
    offer_save_as_new: Literal[False]


class _CatalogTool(TypedDict):
    name: str
    params_schema: dict[str, JsonValue]


class _CatalogHook(TypedDict):
    event: HookEvent
    params_schema: dict[str, JsonValue]
    firing_conditions: list[str]
    wired_on: list[HostFamily]


class Catalog(BaseModel):
    """Available Agent view surfaces and their parameters and host families (D10)."""

    tools: list[_CatalogTool]
    hook_surfaces: list[_CatalogHook]
    host_families: list[HostFamily]


class Preparation(BaseModel):
    """Preparation performed for this emulation (D10)."""

    synced: bool
    adopted_moves: int = Field(ge=0)
    skipped_reason: str | None = None


class Emulation(BaseModel):
    """Exact model-facing text and the context needed to interpret it (D10)."""

    text: str
    emitted: bool
    not_emitted_reason: str | None = None
    chars: int = Field(ge=0)
    budget: int | None = None
    cut: str | None = None
    assumptions: list[str]
    firing_conditions: list[str]
    prepared: Preparation
    ids_in_text: list[str]


@dataclass(frozen=True)
class Route:
    """Owner, authentication and success model of one Portal endpoint (D3–D13)."""

    method: str
    path: str
    auth: Literal["none", "token", "ticket"]
    module: Literal["static", "api_session", "live", "api_read", "api_write", "api_agent"]
    response: type[BaseModel] | None


ROUTES: tuple[Route, ...] = (
    Route("GET", "/", "none", "static", None),
    Route("GET", "/static/{path}", "none", "static", None),
    Route("POST", "/api/live-ticket", "token", "api_session", LiveTicket),
    Route("GET", "/live", "ticket", "live", None),
    Route("GET", "/api/session", "token", "api_session", Session),
    Route("POST", "/api/settings", "token", "api_session", Settings),
    Route("GET", "/api/graph", "token", "api_read", Graph),
    Route("GET", "/api/search", "token", "api_read", SearchResult),
    Route("GET", "/api/records", "token", "api_read", Records),
    Route("GET", "/api/records/{id}", "token", "api_read", RecordCard),
    Route("GET", "/api/records/{id}/anchors/{entity_id}/snippet", "token", "api_read", Snippet),
    Route("GET", "/api/queue", "token", "api_read", Queue),
    Route("POST", "/api/queue/preview", "token", "api_write", PreviewResult),
    Route("POST", "/api/queue/apply", "token", "api_write", ApplyResult),
    Route("POST", "/api/records/{id}/edit", "token", "api_write", OperationResult),
    Route("POST", "/api/records/{id}/edit-and-accept", "token", "api_write", OperationResult),
    Route("POST", "/api/records/{id}/save-as-new", "token", "api_write", OperationResult),
    Route("GET", "/api/agent-view/catalog", "token", "api_agent", Catalog),
    Route("POST", "/api/agent-view/tool", "token", "api_agent", Emulation),
    Route("POST", "/api/agent-view/hook", "token", "api_agent", Emulation),
)
