# Acceptance checklist

Run from a clean project extraction and record the actual machine, Python version, and run duration when benchmarking.

| Requirement | Automated evidence | Manual acceptance |
|---|---|---|
| Configurable synthetic matrix | `test_regenerate_with_different_dimensions_and_rollback_on_failure` | Change SKU/store/day counts in dashboard and verify manifest counts |
| Deterministic seed | Repeated same-seed hash assertion | Generate same small scale twice and compare manifest checksums |
| Safe failed generation | Rollback test | Simulate an invalid configuration; previous manifest and sales file should remain |
| Concurrent generation | Concurrent serialization test | Start two generation requests and confirm no mixed dataset is published |
| SQLite integrity | Generator pre-publish integrity/FK check; validator checks again | Inspect `validation_summary.json` |
| Forecast comparison | End-to-end and API tests | Review MAE/WAPE for four models and confirm selected model is reported |
| No future leakage | Production date-mask and fixture test; forecast target-date validation | Confirm `forecast_origin_date` precedes every target date |
| Censored sales handling | Shared production mask and fixture test | Confirm stockout/censored rows are excluded from training and scoring |
| Forecast-driven replenishment | End-to-end test | Verify recommendation `demand_source` and `lead_time_demand` values |
| MOQ/pack size | Business-rule tests and validator | Every positive quantity is at least MOQ and a pack multiple |
| Lifecycle and lead-time blocks | Production rule tests and validator | Discontinued SKUs and invalid supplier lead times have zero quantity |
| Aggregate store capacity | Capacity decision tests and validator | Sum on-hand + on-order + recommended quantity per store does not exceed capacity |
| Data integrity and checksums | Exact matrix, FK, value, and manifest checks | Full-scale validation will take extra I/O time |
| API and dashboard | API integration test | Run API and Streamlit in separate terminals; inspect all tabs and downloads |
| Full-scale volume | Configurable 2,000 × 100 × 365 generator | Benchmark on target machine; do not claim benchmark completion until actually run |
| Clean distribution | ZIP excludes virtual environments/caches | Extract ZIP into a fresh folder and follow README from scratch |

## Current scope limitations

- Full-scale generation and validation have not been run as part of the normal automated test suite; resource usage depends on the target machine.
- Forecast model selection uses one chronological holdout rather than rolling-origin cross-validation.
- Future promotion dates are not provided as an exogenous calendar; the weekday profile does not know future promotions.
- The project is a local demonstration system, not a secured production service with authentication, role-based access, monitoring, and deployment hardening.
