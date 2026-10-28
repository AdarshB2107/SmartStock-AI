"""Leakage-safe, chunked time-series evaluation and forward forecasts.

Compares an expanding historical mean, a trailing-28-day mean, and a seasonal-naive
(weekday pattern from the last observed week) model. Ground-truth latent demand is
never loaded by this module.
"""
import csv
import json
from collections import defaultdict
import numpy as np
import pandas as pd
from app.config import DATA_DIR
from app.db import connect, record_event
from app.locking import dataset_lock
from app.services.rules import usable_demand_mask, feature_dates_allowed

CHUNK_SIZE = 250_000


def calculate_mae(actual, predicted):
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    return float(np.mean(np.abs(actual - predicted))) if len(actual) else None


def calculate_wape(actual, predicted):
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    denom = float(np.abs(actual).sum())
    return float(np.abs(actual - predicted).sum() / denom) if denom else None


def _metric_record(abs_error, abs_actual, rows):
    return {
        "mae": abs_error / rows if rows else None,
        "wape": abs_error / abs_actual if rows and abs_actual else None,
        "scored_rows": rows,
    }


def run_forecast(holdout_days=14, horizon_days=7):
    """Run evaluation and future forecasts while holding the dataset lock."""
    with dataset_lock():
        return _run_forecast_unlocked(holdout_days=holdout_days, horizon_days=horizon_days)


def _run_forecast_unlocked(holdout_days=14, horizon_days=7):
    if int(holdout_days) < 1 or int(horizon_days) < 1:
        raise ValueError("holdout_days and horizon_days must be positive integers")
    path = DATA_DIR / "sales_history.csv"
    if not path.exists():
        raise RuntimeError("Generate data first from the dashboard or POST /api/generate.")

    min_date = max_date = None
    unique_dates = set()
    for chunk in pd.read_csv(path, usecols=["sale_date"], chunksize=CHUNK_SIZE, parse_dates=["sale_date"]):
        lo, hi = chunk.sale_date.min(), chunk.sale_date.max()
        min_date = lo if min_date is None or lo < min_date else min_date
        max_date = hi if max_date is None or hi > max_date else max_date
        unique_dates.update(chunk.sale_date.dt.strftime("%Y-%m-%d").unique().tolist())
    if min_date is None or max_date is None:
        raise ValueError("Sales history is empty; regenerate the dataset.")

    end = max_date
    total_days = len(unique_dates)
    effective_holdout = min(int(holdout_days), max(0, total_days - 1))
    cutoff = end - pd.Timedelta(days=effective_holdout)
    test_start = cutoff + pd.Timedelta(days=1)
    usecols = ["store_id", "sku_id", "sale_date", "units_sold", "demand_censored_flag", "stockout_flag"]

    sku_df = pd.read_csv(DATA_DIR / "sku.csv", usecols=["sku_id"])
    store_df = pd.read_csv(DATA_DIR / "store.csv", usecols=["store_id"])
    sku_ids = sku_df.sku_id.astype(str).tolist()
    store_ids = store_df.store_id.astype(str).tolist()
    sku_index = {value: idx for idx, value in enumerate(sku_ids)}
    store_index = {value: idx for idx, value in enumerate(store_ids)}
    pair_count = len(sku_ids) * len(store_ids)
    weekday_sums = np.zeros((pair_count, 7), dtype=np.float64)
    weekday_counts = np.zeros((pair_count, 7), dtype=np.int32)

    train_sums = defaultdict(float)
    train_counts = defaultdict(int)
    recent_sums = defaultdict(float)
    recent_counts = defaultdict(int)
    # Store just the final 7 uncensored daily observations per pair, not the full history.
    last_week = {}
    global_sum = 0.0
    global_count = 0
    recent_start = cutoff - pd.Timedelta(days=28)
    weekday_window_start = cutoff - pd.Timedelta(days=56)
    week_start = cutoff - pd.Timedelta(days=6)

    for chunk in pd.read_csv(path, usecols=usecols, chunksize=CHUNK_SIZE, parse_dates=["sale_date"]):
        valid = chunk.loc[usable_demand_mask(chunk)]
        valid = valid.loc[feature_dates_allowed(valid.sale_date, cutoff)]
        if valid.empty:
            continue
        grp = valid.groupby(["store_id", "sku_id"]).units_sold.agg(["sum", "count"])
        for key, row in grp.iterrows():
            train_sums[key] += float(row["sum"])
            train_counts[key] += int(row["count"])
        global_sum += float(valid.units_sold.sum())
        global_count += len(valid)

        recent = valid[valid.sale_date > recent_start]
        if not recent.empty:
            grp_recent = recent.groupby(["store_id", "sku_id"]).units_sold.agg(["sum", "count"])
            for key, row in grp_recent.iterrows():
                recent_sums[key] += float(row["sum"])
                recent_counts[key] += int(row["count"])

        weekday_window = valid[valid.sale_date > weekday_window_start].copy()
        if not weekday_window.empty:
            weekday_window["weekday"] = weekday_window.sale_date.dt.weekday
            weekday_groups = weekday_window.groupby(["store_id", "sku_id", "weekday"]).units_sold.agg(["sum", "count"])
            for (store_id, sku_id, weekday), row in weekday_groups.iterrows():
                pair_idx = store_index[str(store_id)] * len(sku_ids) + sku_index[str(sku_id)]
                weekday_sums[pair_idx, int(weekday)] += float(row["sum"])
                weekday_counts[pair_idx, int(weekday)] += int(row["count"])

        week = valid[valid.sale_date >= week_start]
        for row in week.itertuples(index=False):
            key = (row.store_id, row.sku_id)
            values = last_week.get(key)
            if values is None:
                values = [None] * 7
                last_week[key] = values
            offset = int((row.sale_date - week_start).days)
            if 0 <= offset < 7:
                values[offset] = float(row.units_sold)

    pair_mean = {k: train_sums[k] / count for k, count in train_counts.items() if count}
    global_mean = global_sum / global_count if global_count else 0.0
    recent_mean = {k: recent_sums[k] / count for k, count in recent_counts.items() if count}

    stats = {name: {"abs_error": 0.0, "abs_actual": 0.0, "rows": 0}
             for name in ("expanding_mean", "trailing_28d_mean", "seasonal_naive", "weekday_profile")}
    if effective_holdout > 0:
        for chunk in pd.read_csv(path, usecols=usecols, chunksize=CHUNK_SIZE, parse_dates=["sale_date"]):
            test = chunk.loc[usable_demand_mask(chunk)]
            test = test.loc[(test.sale_date > cutoff) & feature_dates_allowed(test.sale_date, end)]
            if test.empty:
                continue
            actual = test.units_sold.to_numpy(dtype=float)
            expanding = np.fromiter((pair_mean.get((r.store_id, r.sku_id), global_mean)
                                     for r in test.itertuples()), dtype=float, count=len(test))
            trailing = np.fromiter((recent_mean.get((r.store_id, r.sku_id), pair_mean.get((r.store_id, r.sku_id), global_mean))
                                    for r in test.itertuples()), dtype=float, count=len(test))
            seasonal_values = []
            for r in test.itertuples():
                key = (r.store_id, r.sku_id)
                values = last_week.get(key, [None] * 7)
                offset = int((r.sale_date - cutoff).days)
                seasonal_value = values[(offset - 1) % 7] if values else None
                if seasonal_value is None:
                    seasonal_value = recent_mean.get(key, pair_mean.get(key, global_mean))
                seasonal_values.append(float(seasonal_value))
            weekday_values = []
            for r in test.itertuples():
                pair_idx = store_index[str(r.store_id)] * len(sku_ids) + sku_index[str(r.sku_id)]
                wd = int(r.sale_date.weekday())
                if weekday_counts[pair_idx, wd] > 0:
                    weekday_values.append(weekday_sums[pair_idx, wd] / weekday_counts[pair_idx, wd])
                else:
                    weekday_values.append(recent_mean.get((r.store_id, r.sku_id), pair_mean.get((r.store_id, r.sku_id), global_mean)))
            predictions = {"expanding_mean": expanding, "trailing_28d_mean": trailing,
                           "seasonal_naive": np.asarray(seasonal_values, dtype=float),
                           "weekday_profile": np.asarray(weekday_values, dtype=float)}
            for name, pred in predictions.items():
                stats[name]["abs_error"] += float(np.abs(actual - pred).sum())
                stats[name]["abs_actual"] += float(np.abs(actual).sum())
                stats[name]["rows"] += len(test)

    model_metrics = {name: _metric_record(v["abs_error"], v["abs_actual"], v["rows"])
                     for name, v in stats.items()}
    eligible = [name for name, metrics in model_metrics.items() if metrics["scored_rows"] > 0]
    if eligible:
        selected_model = min(eligible, key=lambda name: (
            model_metrics[name]["wape"] if model_metrics[name]["wape"] is not None else float("inf"),
            model_metrics[name]["mae"] if model_metrics[name]["mae"] is not None else float("inf"),
            ["weekday_profile", "seasonal_naive", "trailing_28d_mean", "expanding_mean"].index(name)))
    else:
        selected_model = "trailing_28d_mean"

    forecast_path = DATA_DIR / "forecast.csv"
    fc_count = 0
    origin = end.date().isoformat()
    with open(forecast_path, "w", newline="", encoding="utf-8") as f:
        fields = ["forecast_id", "store_id", "sku_id", "forecast_origin_date", "target_date",
                  "horizon_days", "predicted_demand", "model_name", "created_at"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        batch = []
        for store_id in store_df.store_id:
            for sku_id in sku_df.sku_id:
                key = (store_id, sku_id)
                fallback = recent_mean.get(key, pair_mean.get(key, global_mean))
                week = last_week.get(key, [None] * 7)
                for h in range(1, int(horizon_days) + 1):
                    target = end + pd.Timedelta(days=h)
                    if selected_model == "seasonal_naive":
                        pred = week[(h - 1) % 7] if week else None
                        if pred is None:
                            pred = fallback
                    elif selected_model == "expanding_mean":
                        pred = pair_mean.get(key, global_mean)
                    elif selected_model == "weekday_profile":
                        pair_idx = store_index[str(store_id)] * len(sku_ids) + sku_index[str(sku_id)]
                        wd = int(target.weekday())
                        pred = (weekday_sums[pair_idx, wd] / weekday_counts[pair_idx, wd]
                                if weekday_counts[pair_idx, wd] > 0 else fallback)
                    else:
                        pred = fallback
                    batch.append({
                        "forecast_id": f"FC-{store_id}-{sku_id}-{target.strftime('%Y-%m-%d')}",
                        "store_id": store_id, "sku_id": sku_id,
                        "forecast_origin_date": origin, "target_date": target.date().isoformat(),
                        "horizon_days": h, "predicted_demand": round(max(0.0, float(pred)), 4),
                        "model_name": selected_model, "created_at": origin,
                    })
                    if len(batch) >= 10000:
                        writer.writerows(batch)
                        fc_count += len(batch)
                        batch.clear()
        if batch:
            writer.writerows(batch)
            fc_count += len(batch)

    metrics = {
        "model": selected_model,
        "forecast_origin_date": origin,
        "model_comparison": model_metrics,
        "selection_metric": "lowest holdout WAPE, with MAE as a tie-breaker",
        "train_end_date": cutoff.date().isoformat(),
        "test_start_date": test_start.date().isoformat() if effective_holdout else None,
        "test_end_date": end.date().isoformat() if effective_holdout else None,
        "requested_holdout_days": int(holdout_days), "holdout_days": effective_holdout,
        "horizon_days": int(horizon_days), "forecast_rows": fc_count,
        "scored_rows_excluding_stockout_censored": model_metrics[selected_model]["scored_rows"],
        "mae": model_metrics[selected_model]["mae"], "wape": model_metrics[selected_model]["wape"],
        "evaluation_available": bool(eligible),
        "warnings": ([] if eligible else ["Not enough history for a temporal holdout; forecasts use available uncensored history or zero fallback."]),
        "limitations": [
            "Model selection is based on one chronological holdout, not rolling-origin cross-validation.",
            "The weekday-profile model uses recent uncensored history by weekday; it does not know future promotion calendars.",
            "Censored stockout observations are excluded from scoring; observed sales can understate true demand.",
            "Latent ground-truth demand is never read as a forecasting feature.",
        ],
    }
    (DATA_DIR / "forecast_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if fc_count <= 500_000:
        fc = pd.read_csv(forecast_path)
        with connect() as conn:
            conn.execute("DELETE FROM forecast")
            fc.to_sql("forecast", conn, if_exists="append", index=False, chunksize=10000)
            for row in fc.head(min(50, len(fc))).to_dict("records"):
                record_event(conn, "FORECAST", row["forecast_id"], "FORECAST_CREATED", row, row["created_at"])
    return metrics
