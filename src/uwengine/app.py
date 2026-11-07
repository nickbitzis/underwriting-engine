"""FastAPI service: decisions, what-if, audit lookup, fair lending report, health and metrics."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .engine import EngineError, decide, what_if
from .impact import portfolio_fair_lending
from .llm import assist, get_provider
from .logs import get_logger, log_event
from .metrics import Metrics
from .models import loan_file_from_dict, loan_file_to_dict, to_engine_input
from .pricing import PricingError
from .rules import DEFAULT_RULES_DIR, RulesError, load_all, select_version
from .store import DecisionStore
from .synth import generate_portfolio

APP_ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"


def load_samples(samples_dir: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(samples_dir.glob("*.json")):
        d = json.loads(p.read_text())
        out[d["id"]] = d
    return out


class DecideRequest(BaseModel):
    sample_id: str | None = None
    loan_file: dict | None = None
    rules_version: str | None = None


class WhatIfRequest(DecideRequest):
    variations: list[dict] | None = None
    down_payment_range: dict | None = None  # {"from": 0.10, "to": 0.40, "step": 0.05}


def create_app(
    rules_dir: str | Path | None = None,
    db_path: str | None = None,
    samples_dir: str | Path | None = None,
    provider=None,
) -> FastAPI:
    rules_dir = Path(rules_dir or os.environ.get("UW_RULES_DIR", DEFAULT_RULES_DIR))
    rule_sets = load_all(rules_dir)
    by_version = {r.version: r for r in rule_sets}
    store = DecisionStore(db_path or os.environ.get("UW_DB", str(APP_ROOT / "var" / "decisions.db")))
    samples = load_samples(Path(samples_dir or os.environ.get("UW_SAMPLES_DIR", APP_ROOT / "data" / "samples")))
    provider = provider or get_provider()
    metrics = Metrics()
    log = get_logger()
    app = FastAPI(title="Underwriting Engine", version=__version__)
    app.state.store = store
    app.state.metrics = metrics

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        response = await call_next(request)
        dt = time.perf_counter() - t0
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        metrics.observe_http(path, response.status_code, dt)
        response.headers["x-request-id"] = rid
        log_event(
            log,
            "request",
            request_id=rid,
            method=request.method,
            route=path,
            status=response.status_code,
            duration_ms=round(dt * 1000, 2),
        )
        return response

    @app.exception_handler(EngineError)
    @app.exception_handler(PricingError)
    @app.exception_handler(RulesError)
    async def bad_input(_request: Request, exc: Exception):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    def resolve(req: DecideRequest):
        if req.sample_id:
            if req.sample_id not in samples:
                raise HTTPException(404, f"no sample {req.sample_id}")
            raw = samples[req.sample_id]
        elif req.loan_file:
            raw = req.loan_file
        else:
            raise HTTPException(422, "send sample_id or loan_file")
        try:
            lf = loan_file_from_dict(raw)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid loan file: {e}") from e
        if req.rules_version:
            if req.rules_version not in by_version:
                raise HTTPException(404, f"no rules version {req.rules_version}")
            rules = by_version[req.rules_version]
        else:
            rules = select_version(rule_sets, lf.application_date)
        return lf, rules

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz():
        return {
            "status": "ok",
            "version": __version__,
            "rules_versions": [r.version for r in rule_sets],
            "decisions_stored": store.count(),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        return metrics.render()

    @app.get("/api/samples")
    def list_samples():
        return [
            {
                "id": s["id"],
                "label": s.get("label", s["id"]),
                "program": s["loan"]["program"],
                "purpose": s["loan"]["purpose"],
                "amount": s["loan"]["amount"],
                "state": s["property"]["state"],
            }
            for s in samples.values()
        ]

    @app.get("/api/samples/{sample_id}")
    def get_sample(sample_id: str):
        if sample_id not in samples:
            raise HTTPException(404, f"no sample {sample_id}")
        return loan_file_to_dict(to_engine_input(loan_file_from_dict(samples[sample_id])))

    @app.get("/api/rules")
    def rules_info():
        return [
            {
                "version": r.version,
                "effective_date": str(r.effective_date),
                "sha256": r.sha256,
                "file": r.path,
                "description": r.data.get("description", ""),
            }
            for r in rule_sets
        ]

    @app.post("/decide")
    def post_decide(req: DecideRequest):
        lf, rules = resolve(req)
        inp = to_engine_input(lf)
        d = decide(inp, rules).to_dict()
        store.save(d, loan_file_to_dict(inp))
        if lf.monitoring:
            store.save_monitoring(lf.id, lf.monitoring.protected_group)
        metrics.observe_decision(d["program"], d["outcome"], d["rules_version"])
        log_event(
            log,
            "decision",
            decision_id=d["decision_id"],
            application_id=d["application_id"],
            outcome=d["outcome"],
            rules_version=d["rules_version"],
            note_rate=d["pricing"]["note_rate"],
        )
        return d

    @app.get("/decisions/{decision_id}")
    def get_decision(decision_id: str):
        rec = store.get(decision_id)
        if not rec:
            raise HTTPException(404, f"no decision {decision_id}")
        return rec

    @app.post("/what-if")
    def post_what_if(req: WhatIfRequest):
        lf, rules = resolve(req)
        variations = list(req.variations or [])
        if req.down_payment_range:
            r = req.down_payment_range
            lo, hi, step = float(r.get("from", 0.1)), float(r.get("to", 0.4)), float(r.get("step", 0.05))
            if step <= 0 or hi < lo or (hi - lo) / step > 100:
                raise HTTPException(422, "down_payment_range needs from <= to and at most 100 steps")
            n = int(round((hi - lo) / step)) + 1
            variations += [{"down_payment_pct": round(lo + i * step, 4)} for i in range(n)]
        if not variations or len(variations) > 101:
            raise HTTPException(422, "send 1 to 101 variations")
        rows = what_if(to_engine_input(lf), rules, variations)
        metrics.whatif_scenarios += len(rows)
        return {"application_id": lf.id, "rules_version": rules.version, "scenarios": rows}

    @app.post("/decisions/{decision_id}/assist")
    def post_assist(decision_id: str):
        rec = store.get(decision_id)
        if not rec:
            raise HTTPException(404, f"no decision {decision_id}")
        out = assist(provider, rec)
        metrics.assist_calls[provider.name] += 1
        return out

    @lru_cache(maxsize=1)
    def fair_lending_cached():
        current = select_version(rule_sets, date.today())
        return portfolio_fair_lending(generate_portfolio(300, seed=7), current)

    @app.get("/fair-lending")
    def fair_lending():
        return fair_lending_cached()

    return app
