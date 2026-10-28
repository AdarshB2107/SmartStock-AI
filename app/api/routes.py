from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
import logging
import json
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from app.config import DATA_DIR
from app.locking import dataset_lock
from app.services.generator import generate_data
from app.services.forecasting import run_forecast
from app.services.replenishment import run_replenishment
from app.services.validation import validate_all

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class GenerateRequest(BaseModel):
    n_skus: int = Field(default=50, ge=1, le=2000)
    n_stores: int = Field(default=5, ge=1, le=100)
    n_days: int = Field(default=90, ge=1, le=365)
    seed: int = Field(default=20261022, ge=0, le=2**32 - 1)
    full: bool = False


# Long-running generation jobs run outside the HTTP request so large datasets do not
# exceed browser/proxy request timeouts. The generator itself uses the dataset lock.
_generation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yottaflex-generator")
_generation_jobs: dict[str, dict] = {}
_generation_jobs_lock = threading.Lock()

def _run_generation_job(job_id: str, req: GenerateRequest) -> None:
    with _generation_jobs_lock:
        _generation_jobs[job_id]["status"] = "running"
    try:
        if req.full:
            result = generate_data(seed=req.seed, full=True, load_events_to_db=False)
        else:
            result = generate_data(n_skus=req.n_skus, n_stores=req.n_stores,
                                   n_days=req.n_days, seed=req.seed, full=False)
        with _generation_jobs_lock:
            _generation_jobs[job_id].update({"status": "completed", "manifest": result})
    except Exception as exc:
        logger.exception("Background synthetic data generation failed")
        with _generation_jobs_lock:
            _generation_jobs[job_id].update({
                "status": "failed", "error": f"{type(exc).__name__}: {exc}"
            })



@router.post("/generate-job")
def start_generation_job(req: GenerateRequest):
    """Start generation asynchronously and return immediately with a job ID."""
    with _generation_jobs_lock:
        active = next((j for j in _generation_jobs.values() if j["status"] in {"queued", "running"}), None)
        if active:
            raise HTTPException(status_code=409, detail="A generation job is already running. Wait for it to finish.")
        job_id = uuid.uuid4().hex
        _generation_jobs[job_id] = {"job_id": job_id, "status": "queued"}
    _generation_executor.submit(_run_generation_job, job_id, req)
    return {"job_id": job_id, "status": "queued", "message": "Generation started. Poll /api/generate-job/{job_id} for status."}


@router.get("/generate-job/{job_id}")
def generation_job_status(job_id: str):
    with _generation_jobs_lock:
        job = _generation_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Generation job not found. It may have been started by a previous API process.")
        return dict(job)


@router.post("/generate")
def generate(req: GenerateRequest):
    try:
        if req.full:
            result = generate_data(seed=req.seed, full=True, load_events_to_db=False)
        else:
            result = generate_data(n_skus=req.n_skus, n_stores=req.n_stores,
                                   n_days=req.n_days, seed=req.seed, full=False)
        return {"message": "Synthetic data generated and published", "manifest": result}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Synthetic data generation failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/forecast")
def forecast(holdout_days: int = Query(default=14, ge=1, le=365),
             horizon_days: int = Query(default=7, ge=1, le=365)):
    try:
        return run_forecast(holdout_days=holdout_days, horizon_days=horizon_days)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Forecasting failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/replenishment")
def replenishment():
    try:
        return run_replenishment()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Replenishment failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/validate")
def validate():
    try:
        return validate_all()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Validation failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/summary")
def summary():
    with dataset_lock():
        result = {}
        for name in ["manifest.json", "forecast_metrics.json", "recommendation_summary.json",
                     "validation_summary.json", "ground_truth_results.json"]:
            path = DATA_DIR / name
            if path.exists():
                result[name.replace(".json", "")] = json.loads(path.read_text(encoding="utf-8"))
        manifest = result.get("manifest", {})
        result["row_counts"] = {
            "sku.csv": manifest.get("scale", {}).get("n_skus", 0),
            "store.csv": manifest.get("scale", {}).get("n_stores", 0),
            "sales_history.csv": manifest.get("sales_rows", 0),
            "inventory_snapshot.csv": manifest.get("inventory_rows", 0),
            "forecast.csv": result.get("forecast_metrics", {}).get("forecast_rows", 0),
            "purchase_recommendation.csv": result.get("recommendation_summary", {}).get("recommendation_rows", 0),
        }
        return result


def _filtered_preview(path, filters, limit):
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run the relevant workflow first.")
    count = 0
    items = []
    for chunk in pd.read_csv(path, chunksize=50_000):
        selected = chunk
        for column, value in filters.items():
            if value is not None:
                selected = selected[selected[column].astype(str) == str(value)]
        count += len(selected)
        if len(items) < limit and not selected.empty:
            items.extend(selected.head(limit - len(items)).to_dict(orient="records"))
    return {"count": count, "items": items, "items_limited_to": limit}


@router.get("/recommendations")
def recommendations(status: str | None = None, store_id: str | None = None,
                    limit: int = Query(default=100, ge=1, le=1000)):
    with dataset_lock():
        return _filtered_preview(DATA_DIR / "purchase_recommendation.csv",
                                 {"status": status, "store_id": store_id}, limit)


@router.get("/forecasts")
def forecasts(store_id: str | None = None, sku_id: str | None = None,
              limit: int = Query(default=100, ge=1, le=1000)):
    with dataset_lock():
        return _filtered_preview(DATA_DIR / "forecast.csv", {"store_id": store_id, "sku_id": sku_id}, limit)
