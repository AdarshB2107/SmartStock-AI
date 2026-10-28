"""Forecast-driven replenishment with supplier constraints and store-wide capacity."""
import csv
import json
import math
from collections import defaultdict
import pandas as pd
from app.config import DATA_DIR
from app.db import connect, record_event
from app.locking import dataset_lock
from app.services.rules import is_demand_observation_usable, is_feature_allowed

CHUNK_SIZE = 250_000


def round_order_qty(raw_qty, pack_size, min_order_qty):
    """Round a positive requirement up to both MOQ and a whole pack multiple."""
    pack_size, min_order_qty = int(pack_size), int(min_order_qty)
    if pack_size <= 0 or min_order_qty <= 0:
        raise ValueError("pack_size and min_order_qty must be positive integers")
    raw_qty = max(0, int(math.ceil(float(raw_qty))))
    if raw_qty == 0:
        return 0
    target = max(raw_qty, min_order_qty)
    return int(math.ceil(target / pack_size) * pack_size)


def recommendation_decision(lifecycle_status, lead_time_days, raw_order_qty,
                            pack_size, min_order_qty, capacity_available=None):
    """Production rule used by both replenishment and executable ground-truth fixtures."""
    if str(lifecycle_status).upper() == "DISCONTINUED":
        return {"status": "BLOCKED", "recommended_order_qty": 0, "exception_reason": "DISCONTINUED_SKU"}
    if lead_time_days is None or float(lead_time_days) <= 0:
        return {"status": "BLOCKED", "recommended_order_qty": 0, "exception_reason": "INVALID_LEAD_TIME"}
    raw = max(0, int(math.ceil(float(raw_order_qty))))
    if raw == 0:
        return {"status": "NO_ORDER", "recommended_order_qty": 0, "exception_reason": "INVENTORY_ABOVE_REORDER_POINT"}
    desired = round_order_qty(raw, pack_size, min_order_qty)
    if capacity_available is None:
        return {"status": "ORDER", "recommended_order_qty": desired, "exception_reason": "REORDER_POINT_BREACHED"}
    capacity_available = max(0, int(capacity_available))
    if desired <= capacity_available:
        return {"status": "ORDER", "recommended_order_qty": desired, "exception_reason": "REORDER_POINT_BREACHED"}
    allowed = (capacity_available // int(pack_size)) * int(pack_size)
    if allowed >= int(min_order_qty):
        return {"status": "ORDER", "recommended_order_qty": allowed, "exception_reason": "ORDER_CAPPED_TO_STORE_CAPACITY"}
    return {"status": "CAPACITY_EXCEPTION", "recommended_order_qty": 0,
            "exception_reason": "CAPACITY_BELOW_MOQ_OR_REMAINING_CAPACITY"}


def evaluate_ground_truth():
    """Execute fixtures through the same business-rule functions used in production."""
    gt_path = DATA_DIR / "ground_truth.json"
    if not gt_path.exists():
        return []
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    results = []
    for case in gt:
        fixture, inp, exp = case["fixture"], case["input"], case["expected"]
        if fixture == "stockout_zero_demand":
            usable = is_demand_observation_usable(inp["demand_censored_flag"], inp["stockout_flag"])
            actual = {"censored": not usable, "use_as_zero_demand": usable}
        elif fixture == "pack_size_rounding":
            qty = round_order_qty(inp["raw_order_qty"], inp["pack_size"], inp["min_order_qty"])
            actual = {"recommended_order_qty": qty, "multiple_of_pack_size": qty % inp["pack_size"] == 0}
        elif fixture == "discontinued_sku":
            actual = recommendation_decision(inp["lifecycle_status"], 5, inp["raw_order_qty"], 1, 1)
            actual = {"status": actual["status"], "recommended_order_qty": actual["recommended_order_qty"]}
        elif fixture == "negative_lead_time":
            decision = recommendation_decision("ACTIVE", inp["lead_time_days"], 20, 1, 1)
            actual = {"valid": inp["lead_time_days"] > 0,
                      "recommendation_status": decision["status"]}
        elif fixture == "future_sales_leakage":
            actual = {"feature_is_allowed": is_feature_allowed(inp["feature_sale_date"], inp["forecast_origin_date"])}
        else:
            actual = {}
        results.append({"case_id": case["case_id"], "issue_type": case["issue_type"],
                        "expected": exp, "actual": actual, "passed": actual == exp})
    return results


def _latest_inventory(path):
    latest = {}
    cols = ["store_id", "sku_id", "snapshot_date", "on_hand_units", "on_order_units"]
    for chunk in pd.read_csv(path, usecols=cols, chunksize=CHUNK_SIZE):
        part = chunk.drop_duplicates(["store_id", "sku_id"], keep="last")
        for r in part.itertuples(index=False):
            key = (r.store_id, r.sku_id)
            prev = latest.get(key)
            if prev is None or str(r.snapshot_date) > str(prev["snapshot_date"]):
                latest[key] = {"store_id": r.store_id, "sku_id": r.sku_id,
                               "snapshot_date": r.snapshot_date,
                               "on_hand_units": int(r.on_hand_units), "on_order_units": int(r.on_order_units)}
    return pd.DataFrame(latest.values())


def _demand_means(path):
    sums, counts = defaultdict(float), defaultdict(int)
    cols = ["store_id", "sku_id", "units_sold", "demand_censored_flag", "stockout_flag"]
    for chunk in pd.read_csv(path, usecols=cols, chunksize=CHUNK_SIZE):
        valid_mask = [is_demand_observation_usable(c, s)
                      for c, s in zip(chunk.demand_censored_flag, chunk.stockout_flag)]
        valid = chunk.loc[valid_mask]
        if valid.empty:
            continue
        grp = valid.groupby(["store_id", "sku_id"]).units_sold.agg(["sum", "count"])
        for key, row in grp.iterrows():
            sums[key] += float(row["sum"])
            counts[key] += int(row["count"])
    return pd.DataFrame([{"store_id": key[0], "sku_id": key[1], "avg_daily_demand": sums[key] / counts[key]}
                         for key in sums if counts[key]])


def _forecast_profiles(recommendation_date):
    """Read future forecasts in chunks and keep per-pair daily horizon values."""
    path = DATA_DIR / "forecast.csv"
    metrics_path = DATA_DIR / "forecast_metrics.json"
    if not path.exists() or not metrics_path.exists():
        return {}, 0, "historical_fallback"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    origin = metrics.get("forecast_origin_date") or metrics.get("test_end_date")
    if origin != str(recommendation_date):
        return {}, 0, "historical_fallback_stale_forecast"
    horizon = max(0, int(metrics.get("horizon_days", 0)))
    if not horizon:
        return {}, 0, "historical_fallback"
    profiles = {}
    cols = ["store_id", "sku_id", "horizon_days", "predicted_demand"]
    for chunk in pd.read_csv(path, usecols=cols, chunksize=CHUNK_SIZE):
        for row in chunk.itertuples(index=False):
            h = int(row.horizon_days)
            if 1 <= h <= horizon:
                key = (row.store_id, row.sku_id)
                values = profiles.get(key)
                if values is None:
                    values = [None] * horizon
                    profiles[key] = values
                values[h - 1] = max(0.0, float(row.predicted_demand))
    return profiles, horizon, f"forecast:{metrics.get('model', 'unknown')}"


def _lead_time_demand(profile, horizon, avg_daily_demand, lead_time_days):
    lead = max(0.0, float(lead_time_days))
    if profile is None or horizon <= 0:
        return float(avg_daily_demand) * lead, "historical_fallback"
    covered_days = min(lead, float(horizon))
    whole_days = int(math.floor(covered_days))
    fractional = covered_days - whole_days
    demand = 0.0
    for idx in range(whole_days):
        value = profile[idx] if idx < len(profile) else None
        demand += float(avg_daily_demand) if value is None else float(value)
    if fractional > 0:
        value = profile[whole_days] if whole_days < len(profile) else None
        demand += fractional * (float(avg_daily_demand) if value is None else float(value))
    if lead > horizon:
        demand += (lead - horizon) * float(avg_daily_demand)
        return demand, "forecast_plus_historical_fallback"
    return demand, "forecast"


def run_replenishment():
    with dataset_lock():
        return _run_replenishment_unlocked()


def _run_replenishment_unlocked():
    required = ["sales_history.csv", "inventory_snapshot.csv", "sku.csv", "store.csv",
                "supplier_sku.csv", "reorder_policy.csv", "lead_time_observation.csv"]
    if not all((DATA_DIR / name).exists() for name in required):
        raise RuntimeError("Generate data first.")

    inv = _latest_inventory(DATA_DIR / "inventory_snapshot.csv")
    if inv.empty:
        raise RuntimeError("Inventory snapshots are empty; regenerate the dataset.")
    avg = _demand_means(DATA_DIR / "sales_history.csv")
    skus = pd.read_csv(DATA_DIR / "sku.csv")
    stores = pd.read_csv(DATA_DIR / "store.csv")
    supplier = pd.read_csv(DATA_DIR / "supplier_sku.csv")
    policies = pd.read_csv(DATA_DIR / "reorder_policy.csv")
    lead = pd.read_csv(DATA_DIR / "lead_time_observation.csv")

    recommendation_date = str(inv.snapshot_date.max())
    profiles, forecast_horizon, forecast_source = _forecast_profiles(recommendation_date)
    frame = inv.merge(avg, on=["store_id", "sku_id"], how="left")
    frame["avg_daily_demand"] = frame["avg_daily_demand"].fillna(0.0)
    frame = frame.merge(skus[["sku_id", "lifecycle_status"]], on="sku_id", how="left")
    frame = frame.merge(stores[["store_id", "capacity_units"]], on="store_id", how="left")
    frame = frame.merge(policies[["store_id", "sku_id", "safety_stock_units", "service_level"]],
                        on=["store_id", "sku_id"], how="left")
    frame = frame.merge(supplier[["supplier_sku_id", "sku_id", "pack_size", "min_order_qty", "mean_lead_time_days"]],
                        on="sku_id", how="left")
    if frame["supplier_sku_id"].isna().any() or frame["safety_stock_units"].isna().any():
        raise RuntimeError("Missing supplier or reorder policy for one or more SKU-store pairs.")

    invalid = set(lead.loc[lead.lead_time_days <= 0, "supplier_sku_id"].astype(str))
    frame["inventory_position"] = frame.on_hand_units.astype(int) + frame.on_order_units.astype(int)
    current_by_store = frame.groupby("store_id").inventory_position.sum().to_dict()
    capacity_by_store = stores.set_index("store_id").capacity_units.astype(int).to_dict()
    remaining_capacity = {store_id: max(0, int(capacity_by_store[store_id]) - int(current_by_store.get(store_id, 0)))
                          for store_id in capacity_by_store}

    candidates = []
    for r in frame.itertuples(index=False):
        key = (r.store_id, r.sku_id)
        avg_daily = max(0.0, float(r.avg_daily_demand))
        profile = profiles.get(key)
        lead_demand, demand_source = _lead_time_demand(profile, forecast_horizon, avg_daily, r.mean_lead_time_days)
        position = int(r.inventory_position)
        reorder_point = lead_demand + int(r.safety_stock_units)
        raw_qty = max(0, int(math.ceil(reorder_point - position)))
        decision = recommendation_decision(
            r.lifecycle_status, r.mean_lead_time_days, raw_qty, r.pack_size, r.min_order_qty,
            capacity_available=None
        )
        if str(r.supplier_sku_id) in invalid and decision["status"] != "BLOCKED":
            decision = {"status": "BLOCKED", "recommended_order_qty": 0,
                        "exception_reason": "INVALID_LEAD_TIME_OBSERVATION"}
        candidates.append({
            "store_id": r.store_id, "sku_id": r.sku_id, "supplier_sku_id": r.supplier_sku_id,
            "inventory_position": position, "lead_time_demand": round(lead_demand, 4),
            "reorder_point": round(reorder_point, 4), "raw_order_qty": raw_qty,
            "desired_qty": int(decision["recommended_order_qty"]), "status": decision["status"],
            "exception_reason": decision["exception_reason"], "avg_daily_demand": round(avg_daily, 4),
            "demand_source": demand_source if profile is not None else forecast_source if profiles else "historical_fallback",
            "supplier_lead_time_days": float(r.mean_lead_time_days),
            "pack_size": int(r.pack_size), "min_order_qty": int(r.min_order_qty),
            "lifecycle_status": r.lifecycle_status,
        })

    # Allocate the remaining store capacity across all SKU orders, prioritizing the
    # largest uncovered requirement. This is an aggregate constraint, not a per-SKU split.
    candidates.sort(key=lambda x: (x["store_id"], -x["raw_order_qty"], x["sku_id"]))
    fields = ["recommendation_id", "store_id", "sku_id", "recommendation_date", "supplier_sku_id",
              "inventory_position", "lead_time_demand", "reorder_point", "raw_order_qty",
              "recommended_order_qty", "status", "exception_reason", "created_at", "avg_daily_demand",
              "demand_source", "supplier_lead_time_days", "capacity_before_order", "capacity_after_order"]
    status_counts = {"ORDER": 0, "NO_ORDER": 0, "BLOCKED": 0, "CAPACITY_EXCEPTION": 0}
    total_units = 0
    output_count = 0
    quantities_nonnegative = True
    pack_moq_valid = True
    discontinued_blocked = True
    out_path = DATA_DIR / "purchase_recommendation.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        batch = []
        for counter, item in enumerate(candidates, start=1):
            store_id = item["store_id"]
            remaining_before = int(remaining_capacity.get(store_id, 0))
            status, reason, qty = item["status"], item["exception_reason"], 0
            if status == "ORDER":
                decision = recommendation_decision(
                    item["lifecycle_status"], item["supplier_lead_time_days"], item["raw_order_qty"],
                    item["pack_size"], item["min_order_qty"], capacity_available=remaining_before
                )
                status, reason, qty = decision["status"], decision["exception_reason"], decision["recommended_order_qty"]
                if qty > 0:
                    remaining_capacity[store_id] = remaining_before - qty
            elif status == "NO_ORDER":
                qty = 0
            elif status == "BLOCKED":
                qty = 0
            # For no-order/blocked rows, leave remaining capacity unchanged.
            remaining_after = int(remaining_capacity.get(store_id, 0))
            row = {
                "recommendation_id": f"PO-{recommendation_date[:4]}-{counter:09d}",
                "store_id": store_id, "sku_id": item["sku_id"], "recommendation_date": recommendation_date,
                "supplier_sku_id": item["supplier_sku_id"], "inventory_position": item["inventory_position"],
                "lead_time_demand": item["lead_time_demand"], "reorder_point": item["reorder_point"],
                "raw_order_qty": item["raw_order_qty"], "recommended_order_qty": int(qty),
                "status": status, "exception_reason": reason, "created_at": recommendation_date,
                "avg_daily_demand": item["avg_daily_demand"], "demand_source": item["demand_source"],
                "supplier_lead_time_days": item["supplier_lead_time_days"],
                "capacity_before_order": remaining_before, "capacity_after_order": remaining_after,
            }
            batch.append(row)
            quantities_nonnegative &= int(qty) >= 0
            if int(qty) > 0:
                pack_moq_valid &= int(qty) % int(item["pack_size"]) == 0 and int(qty) >= int(item["min_order_qty"])
            if item["lifecycle_status"] == "DISCONTINUED":
                discontinued_blocked &= status == "BLOCKED" and int(qty) == 0
            status_counts[status] += 1
            total_units += int(qty)
            output_count += 1
            if len(batch) >= 10000:
                writer.writerows(batch)
                batch.clear()
        if batch:
            writer.writerows(batch)

    gt_results = evaluate_ground_truth()
    (DATA_DIR / "ground_truth_results.json").write_text(json.dumps(gt_results, indent=2), encoding="utf-8")
    summary = {
        "recommendation_rows": output_count, "orders": status_counts["ORDER"],
        "no_order": status_counts["NO_ORDER"], "blocked": status_counts["BLOCKED"],
        "capacity_exceptions": status_counts["CAPACITY_EXCEPTION"],
        "total_recommended_units": total_units,
        "demand_source": forecast_source if profiles else "historical_fallback",
        "checks": {
            "nonnegative_quantities": bool(quantities_nonnegative),
            "discontinued_skus_blocked": bool(discontinued_blocked),
            "pack_size_and_moq": bool(pack_moq_valid),
            "ground_truth_cases_passed": sum(x["passed"] for x in gt_results),
            "ground_truth_cases_total": len(gt_results),
        },
        "limitations": [
            "Forecast horizon is extended with the historical uncensored average when supplier lead time exceeds the forecast horizon.",
            "The synthetic service-level field is retained in policy data; safety stock is supplied by the policy table.",
        ],
    }
    (DATA_DIR / "recommendation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if output_count <= 500_000:
        rec = pd.read_csv(out_path)
        with connect() as conn:
            conn.execute("DELETE FROM purchase_recommendation")
            rec.to_sql("purchase_recommendation", conn, if_exists="append", index=False, chunksize=10000)
            for row in rec.head(min(200, len(rec))).to_dict("records"):
                record_event(conn, "PURCHASE_RECOMMENDATION", row["recommendation_id"], "RECOMMENDATION_CREATED", row, row["created_at"])
    return summary
