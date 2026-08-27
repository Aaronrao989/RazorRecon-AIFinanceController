"""FastAPI backend wrapping the standalone reconciliation engine.

Endpoints:
    GET  /                     -> health + config summary
    POST /generate             -> (re)create synthetic datasets
    POST /reconcile            -> kick off a batch job, returns {job_id}
    GET  /progress/{job_id}    -> Server-Sent Events stream of progress
    GET  /status/{job_id}      -> non-streaming status poll (fallback for SSE)
    GET  /results/{job_id}     -> metrics + decisions + exception list
    GET  /audit/{job_id}       -> full audit trail (downloadable JSON)
    GET  /data/{name}          -> download a generated CSV (incl. ground_truth)

The engine is imported as a plain library — this module contains no business
logic, only transport, jobs and streaming.
"""

from __future__ import annotations

import asyncio
import json
import os

# Auto-load backend/.env if present, so GROQ_API_KEY (and friends) don't have to
# be exported in the exact shell that launches uvicorn. Env vars already set in
# the shell still win (load_dotenv does not override by default).
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from recon_engine import generate
from recon_engine.config import (
    AMOUNT_DELTA_CAP,
    CONFIDENCE_THRESHOLD,
    GROQ_MODEL,
    GROQ_MODEL_FALLBACK,
)

from jobs import JobStore

# Where datasets live. Overridable for deployment (Render disk / tmp).
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))

# CORS: comma-separated allowed origins, or "*" for anything (dev default).
_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = ["*"] if _origins_env.strip() == "*" else [
    o.strip() for o in _origins_env.split(",") if o.strip()
]

app = FastAPI(title="AI Finance Controller — Reconciliation API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = JobStore()


class GenerateRequest(BaseModel):
    seed: int = 42
    total: int = 60
    # Append realistic INVESTIGABLE exceptions (split-with-decoy) so an A/B run
    # can demonstrate genuine resolution lift. Appends only; ground truth stays
    # consistent. Idempotent.
    inject_investigable: bool = False


class ReconcileRequest(BaseModel):
    use_llm: bool = True
    # When true, use the clearly-labelled offline simulator instead of calling
    # Groq. Handy for demos without a key. Never presented as a real LLM.
    simulate: bool = False
    # When true, resolve ambiguous records with the tool-calling investigative
    # agent (ReAct loop + read-only tools) instead of a single-shot LLM call.
    use_agent: bool = False


@app.get("/")
def root() -> dict:
    have_key = bool(os.getenv("GROQ_API_KEY"))
    data_ready = os.path.exists(os.path.join(DATA_DIR, "gateway_settlements.csv"))
    return {
        "service": "ai-finance-controller",
        "status": "ok",
        "groq_key_present": have_key,
        "groq_model": GROQ_MODEL,
        "groq_model_fallback": GROQ_MODEL_FALLBACK,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "amount_delta_cap": AMOUNT_DELTA_CAP,
        "data_ready": data_ready,
        "data_dir": DATA_DIR,
    }


@app.post("/generate")
def do_generate(req: GenerateRequest) -> dict:
    paths = generate(DATA_DIR, seed=req.seed, total=req.total)
    injected = None
    if req.inject_investigable:
        from recon_engine.investigability import inject
        injected = inject(DATA_DIR)
    counts = {}
    for name, p in paths.items():
        with open(p, encoding="utf-8") as f:
            counts[name] = max(0, sum(1 for _ in f) - 1)  # minus header
    return {
        "ok": True,
        "paths": {k: os.path.basename(v) for k, v in paths.items()},
        "rows": counts,
        "injected": injected,
    }


@app.post("/reconcile")
def do_reconcile(req: ReconcileRequest) -> dict:
    if not os.path.exists(os.path.join(DATA_DIR, "gateway_settlements.csv")):
        raise HTTPException(status_code=400, detail="No datasets. POST /generate first.")
    job = store.start(DATA_DIR, use_llm=req.use_llm, simulate=req.simulate,
                      use_agent=req.use_agent)
    return {"job_id": job.id, "status": job.status}


@app.post("/compare")
def do_compare(req: ReconcileRequest) -> dict:
    """Synchronous A/B: single-shot resolver vs investigative agent, same batch.

    Returns both metric sets plus the resolution lift (exceptions the agent
    resolved that the single-shot arm could not, and any regressions).
    """
    if not os.path.exists(os.path.join(DATA_DIR, "gateway_settlements.csv")):
        raise HTTPException(status_code=400, detail="No datasets. POST /generate first.")

    from recon_engine import reconcile, SimulatedAgentModel, SimulatedLLMProvider
    from recon_engine.dataio import load_ground_truth

    single = reconcile(
        DATA_DIR,
        provider=SimulatedLLMProvider() if req.simulate else None,
        use_llm=req.use_llm,
    )
    agent = reconcile(
        DATA_DIR,
        agent_model=SimulatedAgentModel() if req.simulate else None,
        use_agent=True,
    )

    truth = load_ground_truth(DATA_DIR)
    s_by = {d.txn_id: d for d in single.decisions}
    a_by = {d.txn_id: d for d in agent.decisions}
    lift, regressions = [], []
    for txn, gt in truth.items():
        s, a = s_by.get(txn), a_by.get(txn)
        if not s or not a:
            continue
        s_ok = s.status.value == gt.true_status
        a_ok = a.status.value == gt.true_status
        if a_ok and not s_ok:
            lift.append({"txn_id": txn, "single": s.status.value,
                         "agent": a.status.value, "truth": gt.true_status})
        elif s_ok and not a_ok:
            regressions.append({"txn_id": txn, "single": s.status.value,
                                "agent": a.status.value, "truth": gt.true_status})

    return {
        "single": single.metrics,
        "agent": agent.metrics,
        "lift": lift,
        "regressions": regressions,
    }


@app.get("/status/{job_id}")
def status(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job_id")
    latest = job.events[-1] if job.events else None
    return {
        "job_id": job.id,
        "status": job.status,
        "events": len(job.events),
        "latest": latest,
        "error": job.error,
    }


@app.get("/progress/{job_id}")
async def progress(job_id: str) -> StreamingResponse:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job_id")

    async def event_stream():
        seq = 0
        # Stream progress events as they arrive; end when the job terminates.
        while True:
            for ev in job.events_since(seq):
                seq = ev["seq"] + 1
                yield f"data: {json.dumps(ev)}\n\n"
            if job.status in ("done", "error") and seq >= len(job.events):
                yield f"data: {json.dumps({'phase': 'stream_end', 'status': job.status})}\n\n"
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/results/{job_id}")
def results(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job.status == "error":
        raise HTTPException(status_code=500, detail=job.error)
    if job.status != "done" or job.result is None:
        return {"job_id": job.id, "status": job.status, "ready": False}
    r = job.result
    return {
        "job_id": job.id,
        "status": job.status,
        "ready": True,
        "metrics": r["metrics"],
        "decisions": r["decisions"],
        "exceptions": r["exceptions"],
    }


@app.get("/audit/{job_id}")
def audit(job_id: str) -> JSONResponse:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if job.status != "done" or job.result is None:
        raise HTTPException(status_code=409, detail="job not finished")
    return JSONResponse(
        content=job.result["audit"],
        headers={"Content-Disposition": f'attachment; filename="audit_{job_id}.json"'},
    )


@app.get("/data/{name}")
def data_file(name: str):
    from fastapi.responses import FileResponse

    safe = os.path.basename(name)
    if not safe.endswith(".csv"):
        raise HTTPException(status_code=400, detail="only .csv files")
    path = os.path.join(DATA_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="text/csv", filename=safe)
