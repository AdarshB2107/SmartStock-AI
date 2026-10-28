"""End-to-end dataset, leakage, business-rule, and artifact validation."""
import hashlib
import json
import numpy as np
import pandas as pd
from app.config import DATA_DIR
from app.db import connect
from app.locking import dataset_lock
from app.services.replenishment import evaluate_ground_truth, _latest_inventory

CHUNK_SIZE = 250_000


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_all():
    """Run all checks while preventing dataset replacement during validation."""
    with dataset_lock():
        return _validate_all_unlocked()


def _validate_all_unlocked():
    errors, checks = [], {}
    needed = ["sku.csv", "store.csv", "supplier.csv", "supplier_sku.csv", "sales_history.csv",
              "inventory_snapshot.csv", "reorder_policy.csv", "lead_time_observation.csv",
              "ground_truth_sales.csv", "ground_truth.json", "manifest.json", "seed_data.sql"]
    missing = [name for name in needed if not (DATA_DIR / name).exists()]
    if missing:
        return {"status": "FAIL", "errors": [f"Missing files: {missing}"], "checks": {}}

    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    n_skus = int(manifest["scale"]["n_skus"])
    n_stores = int(manifest["scale"]["n_stores"])
    n_days = int(manifest["scale"]["n_days"])
    expected_rows = n_skus * n_stores * n_days
    sku_df = pd.read_csv(DATA_DIR / "sku.csv")
    store_df = pd.read_csv(DATA_DIR / "store.csv")
    supplier_df = pd.read_csv(DATA_DIR / "supplier.csv")
    supplier_sku_df = pd.read_csv(DATA_DIR / "supplier_sku.csv")
    policy_df = pd.read_csv(DATA_DIR / "reorder_policy.csv")
    lead_df = pd.read_csv(DATA_DIR / "lead_time_observation.csv")

    for filename, df, pk in [
        ("sku.csv", sku_df, "sku_id"), ("store.csv", store_df, "store_id"),
        ("supplier.csv", supplier_df, "supplier_id"),
        ("supplier_sku.csv", supplier_sku_df, "supplier_sku_id"),
        ("reorder_policy.csv", policy_df, "policy_id"),
        ("lead_time_observation.csv", lead_df, "lead_time_observation_id"),
    ]:
        ok = not df[pk].isna().any() and not df[pk].duplicated().any()
        checks[f"{filename}_unique_pk"] = bool(ok)
        if not ok:
            errors.append(f"Missing or duplicate primary key in {filename}")

    checks["master_row_counts_match_scale"] = (
        len(sku_df) == n_skus and len(store_df) == n_stores and
        len(supplier_sku_df) == n_skus and len(policy_df) == n_skus * n_stores
    )
    if not checks["master_row_counts_match_scale"]:
        errors.append("Master-data row counts do not match configured scale")
    checks["supplier_sku_references_valid"] = bool(
        supplier_sku_df.supplier_id.isin(set(supplier_df.supplier_id)).all() and
        supplier_sku_df.sku_id.isin(set(sku_df.sku_id)).all()
    )
    checks["policy_references_valid"] = bool(
        policy_df.store_id.isin(set(store_df.store_id)).all() and policy_df.sku_id.isin(set(sku_df.sku_id)).all()
    )
    if not checks["supplier_sku_references_valid"] or not checks["policy_references_valid"]:
        errors.append("Master-data foreign-key relationship check failed")

    with connect() as conn:
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks["sqlite_foreign_key_check"] = not fk
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks["sqlite_integrity_check"] = integrity == "ok"
        if fk:
            errors.append(f"SQLite foreign-key violations: {fk[:10]}")
        if integrity != "ok":
            errors.append(f"SQLite integrity check failed: {integrity}")

    store_ids = store_df.store_id.to_numpy(dtype=str)
    sku_ids = sku_df.sku_id.to_numpy(dtype=str)
    dates = pd.date_range(manifest["start_date"], periods=n_days, freq="D").strftime("%Y-%m-%d").to_numpy()
    sales_count = inv_count = 0
    sales_values_ok = inv_values_ok = gt_truth_ok = True
    sales_matrix_exact = inventory_matrix_exact = True
    sales_last_id = inv_last_id = None
    sales_columns = None
    for chunk in pd.read_csv(DATA_DIR / "sales_history.csv", chunksize=CHUNK_SIZE):
        if sales_columns is None:
            sales_columns = set(chunk.columns)
        n = len(chunk)
        positions = np.arange(sales_count, sales_count + n, dtype=np.int64)
        pair_positions = positions // n_days
        store_pos = pair_positions // n_skus
        sku_pos = pair_positions % n_skus
        day_pos = positions % n_days
        sales_matrix_exact &= bool(
            np.array_equal(chunk.store_id.to_numpy(dtype=str), store_ids[store_pos]) and
            np.array_equal(chunk.sku_id.to_numpy(dtype=str), sku_ids[sku_pos]) and
            np.array_equal(chunk.sale_date.to_numpy(dtype=str), dates[day_pos])
        )
        ids = chunk.sale_id.astype(str)
        if not ids.is_monotonic_increasing or (sales_last_id is not None and str(ids.iloc[0]) <= sales_last_id):
            sales_matrix_exact = False
        sales_last_id = str(ids.iloc[-1]) if n else sales_last_id
        sales_values_ok &= bool(
            (chunk.units_sold >= 0).all() and chunk.demand_censored_flag.isin([0, 1]).all() and
            chunk.stockout_flag.isin([0, 1]).all() and
            (chunk.loc[chunk.stockout_flag == 1, "demand_censored_flag"] == 1).all()
        )
        sales_count += n

    for chunk in pd.read_csv(DATA_DIR / "inventory_snapshot.csv", chunksize=CHUNK_SIZE):
        n = len(chunk)
        positions = np.arange(inv_count, inv_count + n, dtype=np.int64)
        pair_positions = positions // n_days
        store_pos = pair_positions // n_skus
        sku_pos = pair_positions % n_skus
        day_pos = positions % n_days
        inventory_matrix_exact &= bool(
            np.array_equal(chunk.store_id.to_numpy(dtype=str), store_ids[store_pos]) and
            np.array_equal(chunk.sku_id.to_numpy(dtype=str), sku_ids[sku_pos]) and
            np.array_equal(chunk.snapshot_date.to_numpy(dtype=str), dates[day_pos])
        )
        ids = chunk.snapshot_id.astype(str)
        if not ids.is_monotonic_increasing or (inv_last_id is not None and str(ids.iloc[0]) <= inv_last_id):
            inventory_matrix_exact = False
        inv_last_id = str(ids.iloc[-1]) if n else inv_last_id
        inv_values_ok &= bool((chunk.on_hand_units >= 0).all() and (chunk.on_order_units >= 0).all())
        inv_count += n

    gt_count = 0
    for chunk in pd.read_csv(DATA_DIR / "ground_truth_sales.csv", chunksize=CHUNK_SIZE):
        gt_count += len(chunk)
        gt_truth_ok &= bool((chunk.true_demand_units > chunk.units_sold).all() and
                            (chunk.true_demand_units >= 0).all() and (chunk.units_sold >= 0).all())
    checks["sales_rows_match_matrix"] = sales_count == expected_rows and sales_count == manifest.get("sales_rows")
    checks["inventory_rows_match_matrix"] = inv_count == expected_rows and inv_count == manifest.get("inventory_rows")
    checks["sales_matrix_exact_and_unique"] = bool(sales_matrix_exact and sales_count == expected_rows)
    checks["inventory_matrix_exact_and_unique"] = bool(inventory_matrix_exact and inv_count == expected_rows)
    checks["sales_values_valid"] = bool(sales_values_ok)
    checks["inventory_values_valid"] = bool(inv_values_ok)
    checks["ground_truth_values_valid"] = bool(gt_truth_ok)
    checks["ground_truth_rows_match_manifest"] = gt_count == manifest.get("censored_ground_truth_rows", gt_count)
    checks["latent_truth_separated_from_model_input"] = "true_demand_units" not in (sales_columns or set())
    if not checks["sales_matrix_exact_and_unique"] or not checks["inventory_matrix_exact_and_unique"]:
        errors.append("Sales/inventory records do not exactly match the expected ordered SKU-store-day matrix")
    if not checks["sales_values_valid"] or not checks["inventory_values_valid"] or not checks["ground_truth_values_valid"]:
        errors.append("Sales, inventory, or latent-ground-truth values violate business constraints")
    if not checks["ground_truth_rows_match_manifest"]:
        errors.append("Ground-truth row count differs from manifest")
    if not checks["latent_truth_separated_from_model_input"]:
        errors.append("Latent true demand leaked into sales_history.csv")

    # Verify all checksums recorded by the generator's manifest. This deliberately
    # streams large files, trading runtime for a meaningful reproducibility check.
    checksum_results = {}
    for filename, metadata in manifest.get("files", {}).items():
        path = DATA_DIR / filename
        ok = path.exists() and _sha256(path) == metadata.get("sha256")
        checksum_results[filename] = bool(ok)
        if not ok:
            errors.append(f"Manifest checksum mismatch or missing file: {filename}")
    checks["manifest_checksums_valid"] = all(checksum_results.values())
    checks["manifest_checksum_files_checked"] = len(checksum_results)

    gt = evaluate_ground_truth()
    (DATA_DIR / "ground_truth_results.json").write_text(json.dumps(gt, indent=2), encoding="utf-8")
    checks["ground_truth_cases_passed"] = sum(bool(row["passed"]) for row in gt)
    checks["ground_truth_cases_total"] = len(gt)
    checks["all_ground_truth_passed"] = bool(gt) and all(row["passed"] for row in gt)
    if not checks["all_ground_truth_passed"]:
        errors.append("One or more production business-rule ground-truth fixtures failed")

    forecast_path = DATA_DIR / "forecast.csv"
    metrics_path = DATA_DIR / "forecast_metrics.json"
    if forecast_path.exists() and metrics_path.exists():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        fc_count, forecast_ok, forecast_origins = 0, True, set()
        for chunk in pd.read_csv(forecast_path, chunksize=CHUNK_SIZE):
            fc_count += len(chunk)
            forecast_ok &= bool((chunk.predicted_demand >= 0).all() and
                                (pd.to_datetime(chunk.target_date) > pd.to_datetime(chunk.forecast_origin_date)).all())
            forecast_origins.update(chunk.forecast_origin_date.astype(str).unique().tolist())
        expected_fc = n_skus * n_stores * int(metrics.get("horizon_days", 0))
        checks["forecast_dates_values_and_origin_valid"] = bool(forecast_ok and forecast_origins == {manifest["end_date"]})
        checks["forecast_row_count_valid"] = fc_count == expected_fc == metrics.get("forecast_rows")
        checks["forecast_model_metrics_present"] = bool(metrics.get("model_comparison"))
        if not checks["forecast_dates_values_and_origin_valid"] or not checks["forecast_row_count_valid"]:
            errors.append("Forecast dates, origin, values, or row counts are invalid")
        if not checks["forecast_model_metrics_present"]:
            errors.append("Forecast model comparison metrics are missing")

    recommendation_path = DATA_DIR / "purchase_recommendation.csv"
    if recommendation_path.exists():
        rec_count, nonnegative, pack_ok, mqo_ok, blocked_ok, status_ok, capacity_trace_ok = 0, True, True, True, True, True, True
        supplier_constraints = supplier_sku_df.set_index("supplier_sku_id")[["pack_size", "min_order_qty"]].to_dict("index")
        dis_ids = set(sku_df.loc[sku_df.lifecycle_status == "DISCONTINUED", "sku_id"])
        rec_chunks = []
        for chunk in pd.read_csv(recommendation_path, chunksize=CHUNK_SIZE):
            rec_count += len(chunk)
            rec_chunks.append(chunk)
            nonnegative &= bool((chunk.recommended_order_qty >= 0).all())
            positive = chunk[chunk.recommended_order_qty > 0]
            for row in positive.itertuples(index=False):
                rule = supplier_constraints.get(row.supplier_sku_id)
                if not rule:
                    pack_ok = mqo_ok = False
                else:
                    pack_ok &= int(row.recommended_order_qty) % int(rule["pack_size"]) == 0
                    mqo_ok &= int(row.recommended_order_qty) >= int(rule["min_order_qty"])
            blocked = chunk[chunk.status == "BLOCKED"]
            blocked_ok &= bool((blocked.recommended_order_qty == 0).all())
            status_ok &= bool(chunk.status.isin(["ORDER", "NO_ORDER", "BLOCKED", "CAPACITY_EXCEPTION"]).all())
            no_order = chunk[chunk.status.isin(["NO_ORDER", "CAPACITY_EXCEPTION"])]
            status_ok &= bool((no_order.recommended_order_qty == 0).all())
            capacity_trace_ok &= bool((chunk.capacity_before_order >= 0).all() and
                                      (chunk.capacity_after_order >= 0).all() and
                                      (chunk.capacity_after_order == chunk.capacity_before_order - chunk.recommended_order_qty).all())
            dis = chunk[chunk.sku_id.isin(dis_ids)]
            blocked_ok &= bool(dis.status.eq("BLOCKED").all() and dis.recommended_order_qty.eq(0).all())
        checks["recommendation_row_count_valid"] = rec_count == n_skus * n_stores
        checks["recommendation_quantities_nonnegative"] = bool(nonnegative)
        checks["positive_orders_respect_pack_and_moq"] = bool(pack_ok and mqo_ok)
        checks["blocked_and_no_order_quantities_zero"] = bool(blocked_ok and status_ok)
        checks["capacity_trace_consistent"] = bool(capacity_trace_ok)
        if not checks["recommendation_row_count_valid"]:
            errors.append("Recommendation row count does not equal SKU-store pairs")
        if not nonnegative or not pack_ok or not mqo_ok or not blocked_ok or not status_ok or not capacity_trace_ok:
            errors.append("Replenishment recommendation rules failed")

        rec_df = pd.concat(rec_chunks, ignore_index=True) if rec_chunks else pd.DataFrame()
        latest_inv = _latest_inventory(DATA_DIR / "inventory_snapshot.csv")
        if not rec_df.empty and not latest_inv.empty:
            base = latest_inv.assign(current_position=latest_inv.on_hand_units + latest_inv.on_order_units).groupby("store_id").current_position.sum()
            order_units = rec_df.groupby("store_id").recommended_order_qty.sum()
            capacity = store_df.set_index("store_id").capacity_units
            after = base.add(order_units, fill_value=0).reindex(capacity.index, fill_value=0)
            checks["aggregate_store_capacity_respected"] = bool((after <= capacity).all())
            if not checks["aggregate_store_capacity_respected"]:
                errors.append("Aggregate on-hand + on-order + recommended units exceed store capacity")

    # A data-only run is not called complete until forecast and replenishment
    # artifacts exist. This prevents a misleading PASS immediately after generation.
    missing_steps = []
    if not (DATA_DIR / "forecast.csv").exists() or not (DATA_DIR / "forecast_metrics.json").exists():
        missing_steps.append("forecast")
    if not recommendation_path.exists():
        missing_steps.append("replenishment")
    workflow_complete = not missing_steps
    bool_checks = [value for value in checks.values() if isinstance(value, (bool, np.bool_))]
    if errors or not all(bool_checks):
        status = "FAIL"
    elif not workflow_complete:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    result = {
        "status": status,
        "workflow_complete": workflow_complete,
        "missing_workflow_steps": missing_steps,
        "errors": errors,
        "checks": checks,
        "row_counts": {"sales_history": sales_count, "inventory_snapshot": inv_count,
                       "ground_truth_sales": gt_count},
        "note": "Sales and inventory are checked against the exact configured SKU-store-day sequence. Large-file checks are streamed; full manifest checksums add I/O time.",
    }
    (DATA_DIR / "validation_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
