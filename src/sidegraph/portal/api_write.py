"""Verdict and edit endpoints for the Portal (D6–D8)."""

from uuid import UUID

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sidegraph.input_limits import InputLimitError, preflight_direct
from sidegraph.store import SupersessionConflictError, git_user_name

from . import cascade, contract, writes
from .context import PortalContext


def routes(ctx: PortalContext) -> list[Route]:
    """Register the write endpoints from the Portal contract.

    See design/superpowers/specs/2026-11-17-portal-design.md (D6–D8).
    """

    def verdicts(accept: list[str], reject: list[str], *, preview: bool) -> JSONResponse:
        with ctx.request_store() as store:
            store.refresh_if_stale()
            if preview:
                nested = writes._nested_marks(store, set(accept) | set(reject))
                accept = [id_ for id_ in accept if id_ not in nested]
                reject = [id_ for id_ in reject if id_ not in nested]
                evidence = writes._batch_evidence(store, set(accept) | set(reject))
                outcomes = dict.fromkeys(sorted(nested), "stays") | cascade.preview(
                    store, accept, reject
                )
                result = contract.PreviewResult.model_validate(
                    {
                        "outcomes": {
                            id_: {"outcome": outcome, "evidence_of": evidence.get(id_)}
                            for id_, outcome in outcomes.items()
                        }
                    }
                )
            else:
                reader = ctx.reader() if ctx.borrowed_from is None else None
                return JSONResponse(
                    writes.apply_verdicts(store, reader, accept, reject).model_dump(
                        mode="json", exclude_none=True
                    )
                )
            return JSONResponse(result.model_dump(mode="json", exclude_none=True))

    async def queue(request: Request) -> JSONResponse:
        from .app import _error

        try:
            body = await request.json()
        except ValueError:
            return _error("invalid", "Expected a JSON object.")
        if not isinstance(body, dict) or body.keys() != {"accept", "reject"}:
            return _error("invalid", "Expected accept and reject lists.")
        accept, reject = body["accept"], body["reject"]
        try:
            preflight_direct(accept, reject)
        except InputLimitError:
            return _error("invalid", "Input exceeds resource limits.")
        for field, values in (("accept", accept), ("reject", reject)):
            if not isinstance(values, list) or not all(isinstance(id_, str) for id_ in values):
                return _error("invalid", "Expected a list of record IDs.", field=field)
        return await run_in_threadpool(
            verdicts, accept, reject, preview=request.url.path.endswith("/preview")
        )

    def edit(
        record_id: str, op_key: str, fields: dict, evidence: list[str], action: str
    ) -> JSONResponse:
        from .app import _error

        with ctx.request_store() as store:
            reader = ctx.reader() if ctx.borrowed_from is None else None
            try:
                author = git_user_name()
                if action == "edit":
                    result = writes.edit_record(
                        store,
                        reader,
                        record_id=record_id,
                        op_key=op_key,
                        fields=fields,
                        carry_facts=evidence,
                        author=author,
                    )
                elif action == "edit-and-accept":
                    result = writes.edit_and_accept(
                        store,
                        reader,
                        draft_id=record_id,
                        op_key=op_key,
                        fields=fields,
                        keep_facts=evidence,
                        author=author,
                    )
                else:
                    result = writes.save_as_new(
                        store,
                        reader,
                        draft_id=record_id,
                        op_key=op_key,
                        fields=fields,
                        author=author,
                    )
            except writes.EditRefusal as exc:
                return _error(exc.code, str(exc), **exc.details)
            except SupersessionConflictError as exc:
                details = {
                    key: value
                    for key, value in (("head", exc.head), ("pending", exc.pending))
                    if value is not None
                }
                return _error(exc.reason, writes._CONFLICT_MESSAGES[exc.reason], **details)
        return JSONResponse(result.model_dump(mode="json", exclude_none=True))

    async def edit_request(request: Request) -> JSONResponse:
        from .app import _error

        try:
            body = await request.json()
        except ValueError:
            return _error("invalid", "Expected a JSON object.")
        action = request.url.path.rsplit("/", 1)[1]
        evidence_key = {"edit": "carry_facts", "edit-and-accept": "keep_facts"}.get(action)
        keys = {"op_key", "fields"}
        if evidence_key is not None:
            keys.add(evidence_key)
        if not isinstance(body, dict) or body.keys() != keys:
            return _error("invalid", "Expected the operation key and edit fields for this route.")
        key, fields = body["op_key"], body["fields"]
        evidence = body[evidence_key] if evidence_key is not None else []
        try:
            if not isinstance(key, str):
                raise ValueError
            op_key = str(UUID(key))
        except ValueError:
            return _error("invalid", "Expected a UUID operation key.", field="op_key")
        if not isinstance(fields, dict):
            return _error("invalid", "Expected a field object.", field="fields")
        if not isinstance(evidence, list) or not all(isinstance(id_, str) for id_ in evidence):
            return _error("invalid", "Expected a list of evidence IDs.", field=evidence_key)
        return await run_in_threadpool(
            edit, request.path_params["id"], op_key, fields, evidence, action
        )

    handlers = {
        "/api/queue/preview": queue,
        "/api/queue/apply": queue,
        "/api/records/{id}/edit": edit_request,
        "/api/records/{id}/edit-and-accept": edit_request,
        "/api/records/{id}/save-as-new": edit_request,
    }
    return [
        Route(route.path, handlers[route.path], methods=[route.method])
        for route in contract.ROUTES
        if route.module == "api_write" and route.path in handlers
    ]
