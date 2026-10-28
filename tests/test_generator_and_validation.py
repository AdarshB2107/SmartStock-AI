import hashlib
import json
from pathlib import Path
import pytest
from app.config import DATA_DIR
from app.services.generator import generate_data
from app.services.forecasting import run_forecast
from app.services.replenishment import run_replenishment
from app.services.validation import validate_all


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_small_dataset_generation_and_incomplete_status():
    manifest = generate_data(n_skus=4, n_stores=2, n_days=10, seed=20261022)
    assert manifest["sales_rows"] == 80
    assert manifest["inventory_rows"] == 80
    report = validate_all()
    assert report["status"] == "INCOMPLETE"
    assert report["missing_workflow_steps"] == ["forecast", "replenishment"]
    assert not report["errors"]
    first_hash = _sha(DATA_DIR / "sales_history.csv")
    generate_data(n_skus=4, n_stores=2, n_days=10, seed=20261022)
    second_hash = _sha(DATA_DIR / "sales_history.csv")
    assert first_hash == second_hash, "Fixed seed should reproduce sales CSV checksum"


def test_regenerate_with_different_dimensions_and_rollback_on_failure():
    first = generate_data(n_skus=3, n_stores=2, n_days=4, seed=7)
    assert first["sales_rows"] == 24
    second = generate_data(n_skus=5, n_stores=3, n_days=6, seed=7)
    assert second["sales_rows"] == 90
    assert second["scale"] == {"n_skus": 5, "n_stores": 3, "n_days": 6}
    previous_hash = _sha(DATA_DIR / "sales_history.csv")
    previous_manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    with pytest.raises(ValueError):
        generate_data(n_skus=0, n_stores=3, n_days=6, seed=7)
    assert _sha(DATA_DIR / "sales_history.csv") == previous_hash
    assert json.loads((DATA_DIR / "manifest.json").read_text())["scale"] == previous_manifest["scale"]


def test_end_to_end_forecast_replenishment_validation():
    manifest = generate_data(n_skus=8, n_stores=3, n_days=21, seed=11)
    forecast = run_forecast(holdout_days=7, horizon_days=5)
    assert forecast["forecast_rows"] == 8 * 3 * 5
    assert set(forecast["model_comparison"]) == {"expanding_mean", "trailing_28d_mean", "seasonal_naive", "weekday_profile"}
    assert forecast["model"] in forecast["model_comparison"]
    recommendations = run_replenishment()
    assert recommendations["recommendation_rows"] == 8 * 3
    assert recommendations["checks"]["ground_truth_cases_passed"] == 5
    report = validate_all()
    assert report["status"] == "PASS", report["errors"]
    assert report["workflow_complete"]
    assert all(value for value in report["checks"].values() if isinstance(value, bool))


def test_concurrent_generation_requests_are_serialized():
    from concurrent.futures import ThreadPoolExecutor
    runs = [(3, 2, 5), (4, 2, 6)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda dims: generate_data(n_skus=dims[0], n_stores=dims[1], n_days=dims[2], seed=31), runs))
    allowed = {tuple(result["scale"][key] for key in ("n_skus", "n_stores", "n_days")) for result in results}
    final_manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    final_scale = tuple(final_manifest["scale"][key] for key in ("n_skus", "n_stores", "n_days"))
    assert final_scale in allowed
    assert final_manifest["sales_rows"] == final_scale[0] * final_scale[1] * final_scale[2]
    assert validate_all()["status"] == "INCOMPLETE"


def test_partial_publish_failure_restores_previous_outputs(tmp_path, monkeypatch):
    import app.services.generator as generator
    target = tmp_path / "data"
    stage = tmp_path / "stage"
    target.mkdir()
    stage.mkdir()
    (target / "sales_history.csv").write_text("old-sales", encoding="utf-8")
    (target / "sku.csv").write_text("old-sku", encoding="utf-8")
    (stage / "sales_history.csv").write_text("new-sales", encoding="utf-8")
    (stage / "sku.csv").write_text("new-sku", encoding="utf-8")

    real_replace = generator.os.replace
    failed_once = {"value": False}

    def fail_one_stage_move(src, dst):
        if Path(src) == stage / "sku.csv" and not failed_once["value"]:
            failed_once["value"] = True
            raise PermissionError("simulated Windows file lock")
        return real_replace(src, dst)

    monkeypatch.setattr(generator.os, "replace", fail_one_stage_move)
    with pytest.raises(RuntimeError, match="previous dataset was restored"):
        generator._publish_staged_dataset(stage, target)
    assert (target / "sales_history.csv").read_text(encoding="utf-8") == "old-sales"
    assert (target / "sku.csv").read_text(encoding="utf-8") == "old-sku"
