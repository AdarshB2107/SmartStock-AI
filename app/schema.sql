PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sku (
  sku_id TEXT PRIMARY KEY,
  sku_name TEXT NOT NULL,
  category TEXT NOT NULL,
  unit_cost REAL NOT NULL CHECK(unit_cost >= 0),
  lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('ACTIVE','DISCONTINUED','LAUNCH')),
  launch_date TEXT NOT NULL,
  discontinue_date TEXT,
  created_at TEXT NOT NULL,
  CHECK(discontinue_date IS NULL OR discontinue_date >= launch_date)
);
CREATE TABLE IF NOT EXISTS store (
  store_id TEXT PRIMARY KEY,
  store_name TEXT NOT NULL,
  region TEXT NOT NULL,
  capacity_units INTEGER NOT NULL CHECK(capacity_units >= 0),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier (
  supplier_id TEXT PRIMARY KEY,
  supplier_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','INACTIVE')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_sku (
  supplier_sku_id TEXT PRIMARY KEY,
  supplier_id TEXT NOT NULL REFERENCES supplier(supplier_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  pack_size INTEGER NOT NULL CHECK(pack_size > 0),
  min_order_qty INTEGER NOT NULL CHECK(min_order_qty > 0),
  mean_lead_time_days REAL NOT NULL CHECK(mean_lead_time_days > 0),
  lead_time_std_days REAL NOT NULL CHECK(lead_time_std_days >= 0),
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  UNIQUE(supplier_id, sku_id, effective_from),
  CHECK(effective_to IS NULL OR effective_to >= effective_from)
);
CREATE TABLE IF NOT EXISTS sales_history (
  sale_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES store(store_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  sale_date TEXT NOT NULL,
  units_sold INTEGER NOT NULL CHECK(units_sold >= 0),
  promotion_flag INTEGER NOT NULL CHECK(promotion_flag IN (0,1)),
  stockout_flag INTEGER NOT NULL CHECK(stockout_flag IN (0,1)),
  demand_censored_flag INTEGER NOT NULL CHECK(demand_censored_flag IN (0,1)),
  issue_type TEXT,
  UNIQUE(store_id, sku_id, sale_date)
);
CREATE TABLE IF NOT EXISTS inventory_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES store(store_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  snapshot_date TEXT NOT NULL,
  on_hand_units INTEGER NOT NULL CHECK(on_hand_units >= 0),
  on_order_units INTEGER NOT NULL CHECK(on_order_units >= 0),
  UNIQUE(store_id, sku_id, snapshot_date)
);
CREATE TABLE IF NOT EXISTS lead_time_observation (
  lead_time_observation_id TEXT PRIMARY KEY,
  supplier_sku_id TEXT NOT NULL REFERENCES supplier_sku(supplier_sku_id),
  observed_date TEXT NOT NULL,
  lead_time_days INTEGER NOT NULL,
  issue_type TEXT,
  ground_truth_label TEXT
);
CREATE TABLE IF NOT EXISTS forecast (
  forecast_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES store(store_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  forecast_origin_date TEXT NOT NULL,
  target_date TEXT NOT NULL,
  horizon_days INTEGER NOT NULL CHECK(horizon_days > 0),
  predicted_demand REAL NOT NULL CHECK(predicted_demand >= 0),
  model_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(store_id, sku_id, forecast_origin_date, target_date, model_name),
  CHECK(target_date > forecast_origin_date)
);
CREATE TABLE IF NOT EXISTS reorder_policy (
  policy_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES store(store_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  safety_stock_units INTEGER NOT NULL CHECK(safety_stock_units >= 0),
  service_level REAL NOT NULL CHECK(service_level > 0 AND service_level < 1),
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  UNIQUE(store_id, sku_id, effective_from),
  CHECK(effective_to IS NULL OR effective_to >= effective_from)
);
CREATE TABLE IF NOT EXISTS purchase_recommendation (
  recommendation_id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES store(store_id),
  sku_id TEXT NOT NULL REFERENCES sku(sku_id),
  recommendation_date TEXT NOT NULL,
  supplier_sku_id TEXT REFERENCES supplier_sku(supplier_sku_id),
  inventory_position INTEGER NOT NULL,
  lead_time_demand REAL NOT NULL,
  reorder_point REAL NOT NULL,
  raw_order_qty INTEGER NOT NULL,
  recommended_order_qty INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ORDER','NO_ORDER','BLOCKED','CAPACITY_EXCEPTION')),
  exception_reason TEXT,
  created_at TEXT NOT NULL,
  avg_daily_demand REAL NOT NULL DEFAULT 0,
  demand_source TEXT NOT NULL DEFAULT 'historical_fallback',
  supplier_lead_time_days REAL NOT NULL DEFAULT 0,
  capacity_before_order INTEGER NOT NULL DEFAULT 0,
  capacity_after_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state_event (
  event_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_time TEXT NOT NULL,
  details_json TEXT NOT NULL
);
