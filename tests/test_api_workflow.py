from fastapi.testclient import TestClient
from app.main import app


def test_api_end_to_end_workflow_and_validation_errors():
    with TestClient(app) as client:
        bad = client.post("/api/generate", json={"n_skus": 0, "n_stores": 2, "n_days": 8})
        assert bad.status_code == 422

        generated = client.post("/api/generate", json={
            "n_skus": 4, "n_stores": 2, "n_days": 12, "seed": 17
        })
        assert generated.status_code == 200, generated.text
        assert generated.json()["manifest"]["sales_rows"] == 96

        forecast = client.post("/api/forecast", params={"holdout_days": 7, "horizon_days": 3})
        assert forecast.status_code == 200, forecast.text
        assert forecast.json()["forecast_rows"] == 4 * 2 * 3
        assert "seasonal_naive" in forecast.json()["model_comparison"]

        replenishment = client.post("/api/replenishment")
        assert replenishment.status_code == 200, replenishment.text
        assert replenishment.json()["recommendation_rows"] == 8

        validation = client.post("/api/validate")
        assert validation.status_code == 200, validation.text
        assert validation.json()["status"] == "PASS", validation.json()

        summary = client.get("/api/summary")
        assert summary.status_code == 200
        assert summary.json()["row_counts"]["sales_history.csv"] == 96
        recommendations = client.get("/api/recommendations", params={"limit": 5})
        assert recommendations.status_code == 200
        assert recommendations.json()["count"] == 8
        assert len(recommendations.json()["items"]) == 5
