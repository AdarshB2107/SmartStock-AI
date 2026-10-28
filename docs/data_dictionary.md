# Data Dictionary and Relational Design

All data is synthetic. Date fields use ISO-8601 `YYYY-MM-DD`; `created_at`/`event_time` use ISO timestamps. Quantities are integer product units unless stated otherwise. IDs are immutable strings. `Required` means non-null in the relational schema unless the field is CSV-only latent ground truth.

## SKU

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `sku_id` | string | Yes | `SKU-000001` format | Primary key |
| `sku_name` | string | Yes | Product display name | Non-empty |
| `category` | string | Yes | grocery, home, personal_care, electronics, apparel | Non-empty |
| `unit_cost` | decimal | Yes | Currency-like synthetic unit cost | `>= 0` |
| `lifecycle_status` | enum | Yes | ACTIVE, DISCONTINUED, LAUNCH | Enum check; discontinued products blocked from replenishment |
| `launch_date` | date | Yes | First eligible selling date | Valid ISO date |
| `discontinue_date` | date | No | Last eligible selling date | Null or `>= launch_date` |
| `created_at` | timestamp | Yes | UTC timestamp | Deterministic for reproducibility |

## Store

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `store_id` | string | Yes | `STORE-000001` format | Primary key |
| `store_name` | string | Yes | Store display name | Non-empty |
| `region` | enum | Yes | North, South, East, West, Central | Enum from generator |
| `capacity_units` | integer | Yes | Total capacity in product units | `>= 0` |
| `created_at` | timestamp | Yes | UTC timestamp | Deterministic |

## SalesHistory (model input; no latent true-demand field)

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `sale_id` | string | Yes | `SALE-<store-id>-<sku-id>-<YYYYMMDD>` | Primary key |
| `store_id` | string | Yes | Existing store ID | FK to Store |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `sale_date` | date | Yes | Observed sales date | One row per store/SKU/date |
| `units_sold` | integer | Yes | Units actually sold | `>= 0`; can be lower than true demand during stockout |
| `promotion_flag` | boolean integer | Yes | 0 or 1 | Enum check |
| `stockout_flag` | boolean integer | Yes | 0 or 1 | 1 when available inventory cannot satisfy demand |
| `demand_censored_flag` | boolean integer | Yes | 0 or 1 | 1 when sales understate demand |
| `issue_type` | string/nullable | No | `STOCKOUT_CENSORED_DEMAND` or null | Label only; not a target demand feature |

Latent demand is intentionally excluded from this input table to avoid leakage.

## InventorySnapshot

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `snapshot_id` | string | Yes | `INV-<store-id>-<sku-id>-<YYYYMMDD>` | Primary key |
| `store_id` | string | Yes | Existing store ID | FK to Store |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `snapshot_date` | date | Yes | Snapshot date | One row per store/SKU/date |
| `on_hand_units` | integer | Yes | Physical available inventory units | `>= 0` |
| `on_order_units` | integer | Yes | Outstanding purchase-order units | `>= 0` |

## Supplier

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `supplier_id` | string | Yes | `SUP-0001` format | Primary key |
| `supplier_name` | string | Yes | Supplier display name | Non-empty |
| `status` | enum | Yes | ACTIVE, INACTIVE | Enum check |
| `created_at` | timestamp | Yes | UTC timestamp | Deterministic |

## SupplierSKU (bridge between Supplier and SKU)

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `supplier_sku_id` | string | Yes | `SUPSKU-000001` | Primary key |
| `supplier_id` | string | Yes | Existing supplier ID | FK to Supplier |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `pack_size` | integer | Yes | Units per supplier pack | `> 0` |
| `min_order_qty` | integer | Yes | MOQ in units | `> 0`; recommendations round up to MOQ and pack multiple |
| `mean_lead_time_days` | decimal | Yes | Expected supplier lead time in days | `> 0` |
| `lead_time_std_days` | decimal | Yes | Lead-time standard deviation in days | `>= 0` |
| `effective_from` | date | Yes | Start date for terms | Valid date |
| `effective_to` | date | No | End date for terms | Null or `>= effective_from` |
| `(supplier_id, sku_id, effective_from)` | composite | Yes | Effective-dated supplier/SKU terms | Unique constraint |

## LeadTimeObservation

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `lead_time_observation_id` | string | Yes | `LTO-000001` | Primary key |
| `supplier_sku_id` | string | Yes | Existing supplier-SKU relationship | FK to SupplierSKU |
| `observed_date` | date | Yes | Date observed | Valid date |
| `lead_time_days` | integer | Yes | Observed days | Negative values retained only as labelled anomaly; block affected recommendation |
| `issue_type` | string/nullable | No | `NEGATIVE_LEAD_TIME` or null | Must explain invalid observation |
| `ground_truth_label` | enum/string | Yes | VALID, INVALID | Explicit anomaly label |

## Forecast

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `forecast_id` | string | Yes | `FC-<store-id>-<sku-id>-<target-date>` | Primary key |
| `store_id` | string | Yes | Existing store ID | FK to Store |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `forecast_origin_date` | date | Yes | Latest date allowed in forecast features | Must be before target date |
| `target_date` | date | Yes | Date being forecast | Must be after origin date |
| `horizon_days` | integer | Yes | Days from origin | `> 0` |
| `predicted_demand` | decimal | Yes | Expected demand in units | `>= 0` |
| `model_name` | string | Yes | `expanding_mean`, `trailing_28d_mean`, `seasonal_naive`, or `weekday_profile` | Non-empty |
| `created_at` | timestamp/date | Yes | Forecast creation date | Deterministic |
| `(store_id, sku_id, origin, target, model)` | composite | Yes | Forecast key | Unique constraint |

## ReorderPolicy

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `policy_id` | string | Yes | `POL-<store-id>-<sku-id>` | Primary key |
| `store_id` | string | Yes | Existing store ID | FK to Store |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `safety_stock_units` | integer | Yes | Buffer stock in units | `>= 0` |
| `service_level` | decimal | Yes | Probability in (0,1), baseline 0.95 | Strictly greater than 0 and less than 1 |
| `effective_from` | date | Yes | Policy start | Valid date |
| `effective_to` | date | No | Policy end | Null or `>= effective_from` |
| `(store_id, sku_id, effective_from)` | composite | Yes | Effective-dated policy | Unique constraint |

## PurchaseRecommendation

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `recommendation_id` | string | Yes | `PO-<year>-000001` format | Primary key |
| `store_id` | string | Yes | Existing store ID | FK to Store |
| `sku_id` | string | Yes | Existing SKU ID | FK to SKU |
| `recommendation_date` | date | Yes | Decision date | Valid date |
| `supplier_sku_id` | string | No | Existing supplier-SKU relationship | FK to SupplierSKU |
| `inventory_position` | integer | Yes | On-hand + on-order units | Derived from inventory snapshot |
| `lead_time_demand` | decimal | Yes | Expected demand over lead time | Sum of forecast demand over the lead-time horizon; historical uncensored average fills any horizon gap |
| `reorder_point` | decimal | Yes | Demand during lead time + safety stock | Derived; not independently randomized |
| `raw_order_qty` | integer | Yes | Unconstrained shortfall | `>= 0` |
| `recommended_order_qty` | integer | Yes | Final constrained quantity | `>= 0`; pack-size multiple and MOQ rules apply |
| `status` | enum | Yes | ORDER, NO_ORDER, BLOCKED, CAPACITY_EXCEPTION | Enum check |
| `exception_reason` | string/nullable | No | Reason for no order/block/capacity issue | Required when an exception is raised by business logic |
| `created_at` | timestamp/date | Yes | Decision creation date | Deterministic |
| `avg_daily_demand` | decimal | Yes | Historical uncensored average daily demand | Fallback and traceability metric |
| `demand_source` | string | Yes | `forecast:<model>`, `forecast_plus_historical_fallback`, or `historical_fallback` | Explains source of lead-time demand estimate |
| `supplier_lead_time_days` | decimal | Yes | Supplier lead-time estimate in days | Must be positive for an actionable order |
| `capacity_before_order` | integer | Yes | Remaining store capacity before this decision | Nonnegative; reflects deterministic recommendation priority order |
| `capacity_after_order` | integer | Yes | Remaining store capacity after this decision | Equals capacity before minus recommended quantity |

## StateEvent

| Field | Type | Required | Allowed values / units | Uniqueness / validation |
|---|---|---:|---|---|
| `event_id` | string | Yes | Deterministic `EVT-...` hash | Primary key |
| `entity_type` | string | Yes | SKU, STORE, FORECAST, PURCHASE_RECOMMENDATION, etc. | Non-empty |
| `entity_id` | string | Yes | ID of the entity being tracked | Checked by application because reference is polymorphic |
| `event_type` | string | Yes | MASTER_CREATED, FORECAST_CREATED, RECOMMENDATION_CREATED, etc. | Non-empty |
| `event_time` | timestamp/date | Yes | Event time | ISO format |
| `details_json` | JSON string | Yes | Event payload | Must be valid JSON; append-only within a generated run |

## Cardinality

- SKU 1:N SalesHistory, InventorySnapshot, Forecast and ReorderPolicy.
- Store 1:N SalesHistory, InventorySnapshot, Forecast and ReorderPolicy.
- Supplier N:M SKU through SupplierSKU; SupplierSKU 1:N LeadTimeObservation.
- Store/SKU 1:N forecasts and purchase recommendations over time.
- StateEvent is an append-only polymorphic event log within a generated run; entity references are validated by application-level checks. To limit database growth, the current implementation records representative forecast/recommendation events (up to 50 forecast rows and 200 recommendation rows per run), not a full event for every exported row.

## Ground truth separation

- `ground_truth_sales.csv` contains latent true demand only for censored sales observations, keyed by `sale_id`; it is not joined into forecast input features.
- `ground_truth.json` contains the five known exception fixtures and expected outcomes.
- `anomaly_cases.csv` documents where each scenario is represented and the expected action.
