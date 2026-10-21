"""HTTP API over the approval service.

Every endpoint except /healthz and the static review page requires an operator bearer
token; the operator identity comes from the token. Without a configured token store the
API refuses all authenticated requests (fail closed). Decisions are recorded only: there
is no endpoint that executes anything.
"""

# No `from __future__ import annotations`: FastAPI must resolve the locally defined
# `Operator` dependency at definition time, or it would be mistaken for a query parameter.

from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hpc_agent.data_adapter import DataAccessError
from hpc_agent.llm.provider import LLMProvider
from hpc_agent.models import ApprovalStatus
from hpc_agent.models.incident import IncidentId
from hpc_agent.safety.approval import InvalidTransitionError
from hpc_agent.safety.guards import SafetyViolation
from hpc_agent.safety.operators import OperatorAuthError, OperatorTokens
from hpc_agent.safety.secrets import SensitiveDataError
from hpc_agent.workflows.approval_workflow import ApprovalService, UnknownRunError

MAX_BODY_BYTES = 16 * 1024
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "app.css": "text/css"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: IncidentId


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED", "REVISION_REQUESTED"]
    comment: str = Field(default="", max_length=1000)


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(default="", max_length=1000)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def create_app(service: ApprovalService, tokens: OperatorTokens | None, llm: LLMProvider | None = None) -> FastAPI:
    app = FastAPI(title="HPC/AI diagnostics POC", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                response: Response = _error(411, "Content-Length required")
            elif int(length) > MAX_BODY_BYTES:
                response = _error(413, "request body too large")
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    # Error bodies never echo request input (it may contain rejected sensitive text).
    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "invalid request", "details": errors})

    @app.exception_handler(UnknownRunError)
    async def _unknown(_: Request, exc: UnknownRunError) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(FileNotFoundError)
    async def _missing(_: Request, exc: FileNotFoundError) -> JSONResponse:
        return _error(404, "not found")

    @app.exception_handler(InvalidTransitionError)
    async def _conflict(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(ValidationError)
    async def _model_invalid(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid input", "details": [
            {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_input=False, include_url=False)]})

    @app.exception_handler(SensitiveDataError)
    async def _sensitive(_: Request, exc: SensitiveDataError) -> JSONResponse:
        return _error(422, "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED.")

    @app.exception_handler(DataAccessError)
    async def _data_access(_: Request, exc: DataAccessError) -> JSONResponse:
        return _error(400, "invalid data reference")

    @app.exception_handler(SafetyViolation)
    async def _violation(_: Request, exc: SafetyViolation) -> JSONResponse:
        return _error(500, "diagnosis rejected by runtime safety checks")

    @app.exception_handler(ValueError)
    async def _bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return _error(422, str(exc))

    def operator(authorization: Annotated[str | None, Header()] = None) -> str:
        if tokens is None:
            raise HTTPException(503, "operator authentication is not configured")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer":
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        try:
            return tokens.authenticate(token.strip())
        except OperatorAuthError:
            raise HTTPException(401, "invalid operator token", headers={"WWW-Authenticate": "Bearer"}) from None

    Operator = Annotated[str, Depends(operator)]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    def static(name: str) -> FileResponse:
        if name not in STATIC_FILES:
            raise HTTPException(404, "not found")
        return FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name])

    @app.get("/api/whoami")
    def whoami(op: Operator) -> dict:
        return {"operator": op}

    @app.get("/api/incidents")
    def incidents(_: Operator) -> list[str]:
        return service.incident_ids()

    @app.get("/api/runs")
    def runs(_: Operator) -> list[dict]:
        return [r.model_dump(mode="json", include={"run_id", "incident_id", "status", "timestamp"})
                for r in service.overview()]

    @app.post("/api/runs", status_code=201)
    def diagnose(body: DiagnoseRequest, _: Operator) -> dict:
        run = service.diagnose(service.load_incident(body.incident_id))
        return {"run_id": run.run_id, "status": service.status(run.run_id).status}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, _: Operator) -> dict:
        return service.runs.load(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: str, _: Operator) -> dict:
        return service.review(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/history")
    def history(run_id: str, _: Operator) -> list[dict]:
        return [r.model_dump(mode="json") for r in service.history(run_id)]

    @app.post("/api/runs/{run_id}/decision")
    def decide(run_id: str, body: DecisionRequest, op: Operator) -> dict:
        record = service.decide(run_id, ApprovalStatus(body.decision), op, body.comment)
        return record.model_dump(mode="json") | {"executed": False}

    @app.post("/api/runs/{run_id}/close")
    def close(run_id: str, body: CommentRequest, op: Operator) -> dict:
        return service.close(run_id, op, body.comment).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/revise", status_code=201)
    def revise(run_id: str, body: CommentRequest, op: Operator) -> dict:
        run = service.revise(run_id, op, body.comment)
        return {"run_id": run.run_id, "supersedes": run.supersedes, "status": service.status(run.run_id).status}

    @app.get("/api/llm")
    def llm_status(_: Operator) -> dict:
        return {"configured": llm is not None, "provider": getattr(llm, "name", None),
                "model": getattr(llm, "model", None)}

    @app.post("/api/runs/{run_id}/explanation", status_code=201)
    def create_explanation(run_id: str, op: Operator) -> dict:
        if llm is None:
            raise HTTPException(503, "no local LLM configured (set MODEL_PROVIDER=ollama)")
        return service.explain(run_id, llm, operator=op).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/explanation")
    def get_explanation(run_id: str, _: Operator) -> dict:
        latest = service.latest_explanation(run_id)
        return {"explanation": latest.model_dump(mode="json") if latest else None}

    @app.get("/api/metrics")
    def workflow_metrics(_: Operator) -> dict:
        from hpc_agent import metrics

        return {"note": metrics.NOT_MTTR,
                "sites": [m.model_dump() for m in metrics.summarize(metrics.collect(service))]}

    @app.get("/api/audit/verify")
    def verify(_: Operator) -> dict:
        return {"audit_records": service.audit.verify(), "approval_records": service.approvals.verify(),
                "chain": "ok"}

    return app
