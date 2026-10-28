"""Deterministic synthetic retail data generator with scalable CSV event exports."""
import csv, hashlib, json, math, random, sqlite3, os, time, shutil, tempfile, uuid
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from contextlib import contextmanager, closing
import numpy as np
import pandas as pd
from app.config import DATA_DIR, DB_PATH, ROOT, SEED, DEFAULT_SCALE, FULL_SCALE
from app.db import initialize, connect, record_event, reset_generated_tables
import app.db as db_module
from app.locking import dataset_lock

CATEGORIES = ["grocery", "home", "personal_care", "electronics", "apparel"]
REGIONS = ["North", "South", "East", "West", "Central"]
START_DATE = date(2025, 1, 1)
LIVE_DATA_DIR = DATA_DIR  # immutable path; DATA_DIR is temporarily redirected during staging

@contextmanager
def _generation_lock():
    """Lock the live data directory before staging and publishing a dataset."""
    with dataset_lock(LIVE_DATA_DIR):
        yield

MANAGED_OUTPUTS = [
    "yottaflex.db", "yottaflex.db-wal", "yottaflex.db-shm",
    "sku.csv", "store.csv", "supplier.csv", "supplier_sku.csv",
    "reorder_policy.csv", "lead_time_observation.csv", "sales_history.csv",
    "inventory_snapshot.csv", "ground_truth_sales.csv", "ground_truth.json",
    "anomaly_cases.csv", "seed_data.sql", "manifest.json", "validation_summary.json",
    "forecast.csv", "forecast_metrics.json", "purchase_recommendation.csv",
    "recommendation_summary.json", "ground_truth_results.json",
]

def _publish_staged_dataset(stage_dir: Path, target_dir: Path):
    """Publish a complete staged dataset, rolling back if any replacement fails."""
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = target_dir.parent / f".{target_dir.name}.backup-{uuid.uuid4().hex}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    moved_old, moved_new = [], []
    stage_names = {p.name for p in stage_dir.iterdir() if p.is_file()}
    try:
        # Move the old generation out of the way first. The application lock
        # ensures our API workers cannot be reading these files during publish.
        for name in MANAGED_OUTPUTS:
            old = target_dir / name
            if old.exists():
                os.replace(old, backup_dir / name)
                moved_old.append(name)
        for name in sorted(stage_names):
            os.replace(stage_dir / name, target_dir / name)
            moved_new.append(name)
    except Exception as exc:
        # Remove partially published files and restore the last known-good run.
        rollback_ok = True
        for name in moved_new:
            try:
                (target_dir / name).unlink(missing_ok=True)
            except OSError:
                rollback_ok = False
        for name in reversed(moved_old):
            backup = backup_dir / name
            if backup.exists():
                try:
                    os.replace(backup, target_dir / name)
                except OSError:
                    rollback_ok = False
        if rollback_ok:
            shutil.rmtree(backup_dir, ignore_errors=True)
        raise RuntimeError(
            "Could not publish the generated dataset safely. The previous dataset was restored where possible. "
            "Stop other processes that may have the SQLite database open and try again. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)


def generate_data(n_skus=None, n_stores=None, n_days=None, seed=SEED, full=False, load_events_to_db=True):
    """Generate into a staging directory, validate, then publish as one protected run.

    The previous valid outputs remain available if generation fails before publish.
    Database and CSV output files are only switched after all generator handles close.
    """
    live_data_dir = LIVE_DATA_DIR
    with _generation_lock():
        stage_root = Path(tempfile.mkdtemp(prefix=f".{live_data_dir.name}.stage-", dir=live_data_dir.parent))
        stage_data_dir = stage_root / "data"
        stage_data_dir.mkdir(parents=True, exist_ok=True)
        original_generator_data, original_generator_db = DATA_DIR, DB_PATH
        original_db_data, original_db_path = db_module.DATA_DIR, db_module.DB_PATH
        try:
            try:
                # Existing functions keep their simple DATA_DIR interface, while the
                # staging override makes all output and SQLite writes isolated.
                globals()["DATA_DIR"] = stage_data_dir
                globals()["DB_PATH"] = stage_data_dir / "yottaflex.db"
                db_module.DATA_DIR = stage_data_dir
                db_module.DB_PATH = stage_data_dir / "yottaflex.db"
                manifest = _generate_data_unlocked(
                    n_skus=n_skus, n_stores=n_stores, n_days=n_days, seed=seed,
                    full=full, load_events_to_db=load_events_to_db
                )
                # Check database integrity before making this generation visible.
                # sqlite3's context manager commits/rolls back but does NOT close
                # the handle; closing() is essential before moving the DB on Windows.
                with closing(sqlite3.connect(stage_data_dir / "yottaflex.db")) as check_conn:
                    integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
                    fk_errors = check_conn.execute("PRAGMA foreign_key_check").fetchall()
                if integrity != "ok" or fk_errors:
                    raise RuntimeError(f"Staged SQLite integrity check failed: {integrity}; foreign keys: {fk_errors[:5]}")
            finally:
                globals()["DATA_DIR"] = original_generator_data
                globals()["DB_PATH"] = original_generator_db
                db_module.DATA_DIR = original_db_data
                db_module.DB_PATH = original_db_path
            _publish_staged_dataset(stage_data_dir, live_data_dir)
            return manifest
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

def sid(prefix, n, width=6):
    return f"{prefix}-{n:0{width}d}"

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _insert_many(conn, table, columns, rows):
    if not rows:
        return
    marks = ",".join(["?"] * len(columns))
    conn.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", rows)

def _generate_data_unlocked(n_skus=None, n_stores=None, n_days=None, seed=SEED, full=False, load_events_to_db=True):
    scale = FULL_SCALE if full else DEFAULT_SCALE
    n_skus = int(scale["n_skus"] if n_skus is None else n_skus)
    n_stores = int(scale["n_stores"] if n_stores is None else n_stores)
    n_days = int(scale["n_days"] if n_days is None else n_days)
    if min(n_skus, n_stores, n_days) < 1:
        raise ValueError("n_skus, n_stores and n_days must be positive integers")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # This function writes only into an isolated staging directory. It starts
    # from a fresh SQLite file; the live dataset is untouched until publish.
    initialize()
    reset_generated_tables()
    rng = np.random.default_rng(seed)
    random.seed(seed)
    now = datetime.combine(START_DATE, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    end_date = START_DATE + timedelta(days=n_days-1)

    skus, stores, suppliers, supplier_skus, policies, lead_obs = [], [], [], [], [], []
    for i in range(1, n_skus+1):
        launch_day = int(rng.integers(0, max(1, min(n_days, 60))))
        launch = START_DATE + timedelta(days=launch_day)
        # Ensure the starter dataset exercises the discontinued-SKU exception too.
        status = "DISCONTINUED" if (i % 97 == 0 or (i == n_skus and n_skus >= 10)) else ("LAUNCH" if i % 17 == 0 else "ACTIVE")
        disc = max(launch, end_date - timedelta(days=2)).isoformat() if status == "DISCONTINUED" and end_date >= launch else None
        skus.append((sid("SKU",i), f"Product {i:06d}", CATEGORIES[(i-1)%len(CATEGORIES)],
                     round(float(rng.uniform(2,250)),2), status, launch.isoformat(), disc, now))
    for i in range(1,n_stores+1):
        stores.append((sid("STORE",i), f"Store {i:06d}", REGIONS[(i-1)%len(REGIONS)],
                        int(rng.integers(max(3000,n_skus*60), max(5000,n_skus*100))), now))
    n_suppliers = max(3, min(30, n_skus))
    suppliers = [(sid("SUP",i,4), f"Supplier {i:04d}", "ACTIVE", now) for i in range(1,n_suppliers+1)]
    for i, sku in enumerate(skus,1):
        sup = suppliers[(i-1)%len(suppliers)]
        pack = int(rng.choice([1,4,6,12,24]))
        moq = pack * int(rng.choice([1,1,2,3]))
        mean_lt = round(float(rng.uniform(2,14)),2)
        supplier_skus.append((f"SUPSKU-{i:06d}", sup[0], sku[0], pack, moq, mean_lt,
                              round(float(rng.uniform(.5,3)),2), START_DATE.isoformat(), None))
    for store in stores:
        for sku in skus:
            policies.append((f"POL-{store[0]}-{sku[0]}", store[0], sku[0],
                             int(rng.integers(2,20)), .95, START_DATE.isoformat(), None))

    with connect() as conn:
        _insert_many(conn,"sku","sku_id,sku_name,category,unit_cost,lifecycle_status,launch_date,discontinue_date,created_at".split(","),skus)
        _insert_many(conn,"store","store_id,store_name,region,capacity_units,created_at".split(","),stores)
        _insert_many(conn,"supplier","supplier_id,supplier_name,status,created_at".split(","),suppliers)
        _insert_many(conn,"supplier_sku","supplier_sku_id,supplier_id,sku_id,pack_size,min_order_qty,mean_lead_time_days,lead_time_std_days,effective_from,effective_to".split(","),supplier_skus)
        _insert_many(conn,"reorder_policy","policy_id,store_id,sku_id,safety_stock_units,service_level,effective_from,effective_to".split(","),policies)
        # Deliberate invalid lead-time anomaly is retained and labelled for validation, not silently repaired.
        for i, ss in enumerate(supplier_skus,1):
            observed = max(1, int(rng.normal(ss[5], ss[6])))
            issue, label = None, "VALID"
            if i == 1:
                observed, issue, label = -2, "NEGATIVE_LEAD_TIME", "INVALID"
            lead_obs.append((f"LTO-{i:06d}", ss[0], (START_DATE+timedelta(days=min(30,n_days-1))).isoformat(), observed, issue, label))
        _insert_many(conn,"lead_time_observation","lead_time_observation_id,supplier_sku_id,observed_date,lead_time_days,issue_type,ground_truth_label".split(","),lead_obs)
        for sku in skus:
            record_event(conn, "SKU", sku[0], "MASTER_CREATED", {"lifecycle_status":sku[4]}, now)
        for store in stores:
            record_event(conn, "STORE", store[0], "MASTER_CREATED", {"region":store[2]}, now)

    # Stream daily events to CSV, never keep the 73M-row full dataset in RAM.
    sales_path, inv_path = DATA_DIR/"sales_history.csv", DATA_DIR/"inventory_snapshot.csv"
    sales_fields = ["sale_id","store_id","sku_id","sale_date","units_sold","promotion_flag","stockout_flag",
                    "demand_censored_flag","issue_type"]
    inv_fields = ["snapshot_id","store_id","sku_id","snapshot_date","on_hand_units","on_order_units"]
    gt_fields = ["sale_id","units_sold","true_demand_units","issue_type"]
    sales_count = inv_count = gt_sales_count = 0
    batch_size = 10000
    with open(sales_path,"w",newline="",encoding="utf-8") as sf, open(inv_path,"w",newline="",encoding="utf-8") as inf, open(DATA_DIR/"ground_truth_sales.csv","w",newline="",encoding="utf-8") as gf:
        sw, iw, gw = csv.DictWriter(sf,fieldnames=sales_fields), csv.DictWriter(inf,fieldnames=inv_fields), csv.DictWriter(gf,fieldnames=gt_fields)
        sw.writeheader(); iw.writeheader(); gw.writeheader()
        sb, ib, gb = [], [], []
        for store in stores:
            for sku_idx, sku in enumerate(skus,1):
                base = float(rng.uniform(.05,8.0))
                stock = int(rng.integers(0,60))
                for di in range(n_days):
                    d = START_DATE + timedelta(days=di)
                    weekly = 1.0 + .2*math.sin(2*math.pi*d.weekday()/7)
                    seasonal = 1.0 + .25*math.sin(2*math.pi*di/365.0)
                    promo = int(rng.random() < .06)
                    launch_factor = 1.25 if d.isoformat() >= sku[5] else 0.0
                    demand = int(rng.poisson(max(.01,base*weekly*seasonal*(1.8 if promo else 1.0)*launch_factor)))
                    if sku[4] == "DISCONTINUED" and sku[6] and d.isoformat() > sku[6]:
                        demand = 0
                    # Ensure stockout-censoring occurs often enough for evaluation.
                    sold = min(stock,demand)
                    stockout = int(sold < demand)
                    censored = int(sold < demand)
                    issue = "STOCKOUT_CENSORED_DEMAND" if censored else None
                    sale_id=f"SALE-{store[0]}-{sku[0]}-{d.strftime('%Y%m%d')}"
                    sb.append((sale_id,store[0],sku[0],d.isoformat(),sold,promo,stockout,censored,issue))
                    if censored:
                        gb.append((sale_id,sold,demand,"STOCKOUT_CENSORED_DEMAND"))
                    stock = max(0,stock-sold)
                    if di % 7 == 0:
                        stock += int(rng.integers(0,15))
                    on_order = int(rng.integers(0,20))
                    ib.append((f"INV-{store[0]}-{sku[0]}-{d.strftime('%Y%m%d')}",store[0],sku[0],d.isoformat(),stock,on_order))
                    if len(sb) >= batch_size:
                        sw.writerows(dict(zip(sales_fields,r)) for r in sb)
                        iw.writerows(dict(zip(inv_fields,r)) for r in ib)
                        sales_count += len(sb); inv_count += len(ib); sb.clear(); ib.clear()
                    if len(gb) >= batch_size:
                        gw.writerows(dict(zip(gt_fields,r)) for r in gb)
                        gt_sales_count += len(gb); gb.clear()
        if sb:
            sw.writerows(dict(zip(sales_fields,r)) for r in sb)
            iw.writerows(dict(zip(inv_fields,r)) for r in ib)
            sales_count += len(sb); inv_count += len(ib)
        if gb:
            gw.writerows(dict(zip(gt_fields,r)) for r in gb)
            gt_sales_count += len(gb)

    matrix_rows = n_skus*n_stores*n_days
    # Keep SQLite as a queryable event store for smaller runs. CSV remains the source
    # of truth for larger runs to avoid expensive database inserts and excessive RAM use.
    sqlite_event_limit = 100_000
    if load_events_to_db and matrix_rows <= sqlite_event_limit:
        sales_df = pd.read_csv(sales_path)
        inv_df = pd.read_csv(inv_path)
        with connect() as conn:
            sales_df.to_sql("sales_history",conn,if_exists="append",index=False,chunksize=10000)
            inv_df.to_sql("inventory_snapshot",conn,if_exists="append",index=False,chunksize=10000)

    # Persist master and policy data exports.
    with connect() as conn:
        for table in ["sku","store","supplier","supplier_sku","reorder_policy","lead_time_observation"]:
            pd.read_sql_query(f"SELECT * FROM {table}",conn).to_csv(DATA_DIR/f"{table}.csv",index=False)

    # Separate scenario catalogue used by evaluators; this file is never joined into model features.
    anomaly_cases = [
      {"case_id":"GT-001","issue_type":"STOCKOUT_MISTAKEN_FOR_ZERO_DEMAND","fixture":"stockout_zero_demand","injected_into_baseline":"stockout flags and demand_censored_flag on sales rows","expected_action":"exclude censored zero sales from naive demand estimation"},
      {"case_id":"GT-002","issue_type":"REORDER_QUANTITY_IGNORES_PACK_SIZE","fixture":"pack_size_rounding","injected_into_baseline":"deterministic unit test fixture","expected_action":"round order up to MOQ and pack multiple"},
      {"case_id":"GT-003","issue_type":"DISCONTINUED_SKU_RECOMMENDED","fixture":"discontinued_sku","injected_into_baseline":"discontinued SKU master rows where scale includes SKU-000097","expected_action":"block replenishment"},
      {"case_id":"GT-004","issue_type":"NEGATIVE_LEAD_TIME","fixture":"negative_lead_time","injected_into_baseline":"LTO-000001 observation with -2 days and label INVALID","expected_action":"block affected supplier-SKU recommendation"},
      {"case_id":"GT-005","issue_type":"FORECAST_USES_FUTURE_SALES","fixture":"future_sales_leakage","injected_into_baseline":"deterministic unit test fixture","expected_action":"reject any feature timestamp later than forecast origin"}
    ]
    (DATA_DIR/"anomaly_cases.csv").write_text(pd.DataFrame(anomaly_cases).to_csv(index=False),encoding="utf-8")
    ground_truth = [
      {"case_id":"GT-001","issue_type":"STOCKOUT_MISTAKEN_FOR_ZERO_DEMAND","fixture":"stockout_zero_demand",
       "input":{"units_sold":0,"true_demand_units":7,"stockout_flag":1,"demand_censored_flag":1},
       "expected":{"censored":True,"use_as_zero_demand":False}},
      {"case_id":"GT-002","issue_type":"REORDER_QUANTITY_IGNORES_PACK_SIZE","fixture":"pack_size_rounding",
       "input":{"raw_order_qty":13,"pack_size":6,"min_order_qty":12},
       "expected":{"recommended_order_qty":18,"multiple_of_pack_size":True}},
      {"case_id":"GT-003","issue_type":"DISCONTINUED_SKU_RECOMMENDED","fixture":"discontinued_sku",
       "input":{"lifecycle_status":"DISCONTINUED","raw_order_qty":20},
       "expected":{"status":"BLOCKED","recommended_order_qty":0}},
      {"case_id":"GT-004","issue_type":"NEGATIVE_LEAD_TIME","fixture":"negative_lead_time",
       "input":{"lead_time_days":-2},
       "expected":{"valid":False,"recommendation_status":"BLOCKED"}},
      {"case_id":"GT-005","issue_type":"FORECAST_USES_FUTURE_SALES","fixture":"future_sales_leakage",
       "input":{"forecast_origin_date":"2025-03-01","feature_sale_date":"2025-03-02"},
       "expected":{"feature_is_allowed":False}}
    ]
    (DATA_DIR/"ground_truth.json").write_text(json.dumps(ground_truth,indent=2),encoding="utf-8")
    # SQL seed dump for reproducible master-data inspection. For small datasets,
    # preserve the complete database dump. For larger matrices, do not serialize
    # millions of fact-table INSERT statements: the event CSVs are the canonical
    # source and are already included separately.
    with connect() as conn:
        if matrix_rows <= 10_000:
            seed_sql = "\n".join(conn.iterdump())
        else:
            lines = ["-- Large-run seed: schema and master data only.",
                     "-- sales_history and inventory_snapshot event data are in the matching CSV files."]
            tables = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for table_name, create_sql in tables:
                if create_sql:
                    lines.append(create_sql + ";")
            indexes = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
            for (index_sql,) in indexes:
                lines.append(index_sql + ";")
            master_tables = ["sku", "store", "supplier", "supplier_sku",
                             "reorder_policy", "lead_time_observation"]
            def sql_value(value):
                if value is None: return "NULL"
                if isinstance(value, (int, float)): return str(value)
                if isinstance(value, bytes): return "X'" + value.hex() + "'"
                return "'" + str(value).replace("'", "''") + "'"
            for table_name in master_tables:
                columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
                if not columns: continue
                quoted_columns = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
                for row in conn.execute(f'SELECT * FROM "{table_name}"'):
                    values = ",".join(sql_value(v) for v in row)
                    lines.append(f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({values});')
            seed_sql = "\n".join(lines)
        (DATA_DIR/"seed_data.sql").write_text(seed_sql, encoding="utf-8")
    # source manifest and checksums
    manifest = {"seed":seed,"start_date":START_DATE.isoformat(),"end_date":end_date.isoformat(),
      "scale":{"n_skus":n_skus,"n_stores":n_stores,"n_days":n_days},
      "sku_store_pairs":n_skus*n_stores,"sales_rows":sales_count,"inventory_rows":inv_count,"censored_ground_truth_rows":gt_sales_count,
      "event_records_loaded_into_sqlite": bool(load_events_to_db and matrix_rows <= 100_000),
      "seed_sql_includes_event_rows": bool(matrix_rows <= 10_000),
      "synthetic_only":True}
    for name in ["sku.csv","store.csv","supplier.csv","supplier_sku.csv","reorder_policy.csv",
                 "lead_time_observation.csv","sales_history.csv","inventory_snapshot.csv","ground_truth_sales.csv","ground_truth.json","anomaly_cases.csv","seed_data.sql"]:
        p=DATA_DIR/name
        manifest.setdefault("files",{})[name]={"bytes":p.stat().st_size,"sha256":file_sha256(p)}
    (DATA_DIR/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    return manifest
