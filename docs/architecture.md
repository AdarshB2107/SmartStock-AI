# Architecture and data flow

```text
Configuration + seed
        |
        v
Synthetic generator -- master tables + sales/inventory CSV + ground truth
        |                         |
        |                         +--> manifest + SHA-256 checksums
        v
Temporary staging directory --> SQLite integrity/FK checks --> protected publish
        |
        v
Forecasting: chronological split --> uncensored training rows --> model comparison
        |                                                  |
        +--> forecast.csv + forecast_metrics.json <--------+
        |
        v
Replenishment: latest inventory + selected forecast + policy + supplier constraints
        |
        +--> purchase_recommendation.csv + recommendation_summary.json
        |
        v
Validation: row matrix, IDs, values, relationships, leakage, business rules, checksums
        |
        +--> validation_summary.json
        |
        v
FastAPI endpoints <--> Streamlit dashboard
```

## Reliability model

- All dataset operations use a cross-process lock file in `data/.generation.lock`.
- Generation writes to a fresh staging directory rather than mutating the active dataset.
- The staged SQLite file is checked for integrity and foreign-key violations before publication.
- Publication moves the prior known output files into a backup directory, installs the staged outputs, and attempts rollback if a replacement fails.
- Forecast, replenishment, validation, and API file reads acquire the same lock to avoid reading while the API publishes a new dataset.
- The Streamlit UI only requests data through the API for workflow operations; it reads exported CSVs for previews after API operations return. Avoid opening/replacing the database through an external SQLite tool while the app is running.

## Large data design

The SKU-store-day event matrix is streamed into CSV. For smaller runs, event records are also loaded into SQLite for convenient inspection. At larger scales, CSV is the canonical event store to avoid loading tens of millions of rows into SQLite or memory. Forecasting and validation use chunked reads; master tables and per-pair forecast/recommendation summaries are much smaller than daily event history.
