# Forecast model evaluation

## Models compared

1. **Expanding mean** — mean of all uncensored training observations for each store-SKU pair; falls back to the global training mean when a pair has no usable training observations.
2. **Trailing-28-day mean** — mean of uncensored observations in the most recent 28 days before the forecast cutoff; falls back to the expanding pair/global mean.
3. **Seasonal naive** — repeats the final seven uncensored training observations as a weekday pattern; missing/censored weekdays fall back to the trailing pair/global mean.
4. **Weekday profile** — mean of uncensored observations for the corresponding weekday in a recent 56-day training window; missing weekdays fall back to the trailing pair/global mean.

## Evaluation protocol

- The split is chronological. The forecast origin is the last observed date and the holdout is immediately before that origin; only observations at or before the training cutoff contribute to model estimates.
- Stockout-censored sales observations are excluded from training statistics and evaluation because observed sales may be lower than latent demand.
- **MAE** is the mean absolute difference between observed sales and predictions.
- **WAPE** is total absolute error divided by total absolute observed units. If the holdout contains no observed units, WAPE is undefined and the tie-breaker is MAE.
- The selected model is the one with the lowest available holdout WAPE, using MAE as a tie-breaker. All model scores are persisted in `data/forecast_metrics.json`.

## Interpretation and limitations

- Results are specific to the synthetic dataset and the chosen seed; they do not guarantee performance on real retail data.
- One holdout can be noisy. Use rolling-origin backtesting before using the scores for a business decision.
- The generator includes promotions, but no future promotion calendar is supplied to forecasting. The weekday-profile model captures recent weekday patterns but does not explicitly estimate promotion uplift.
- Latent `true_demand_units` is held in `ground_truth_sales.csv` for anomaly analysis and is not a forecasting feature.
- Forecast horizon beyond the model's observed history uses the selected model's defined fallback, not future actual sales.

## Example from the bundled sample run

Configuration: seed `20261022`, 50 SKUs × 5 stores × 90 days, 14-day chronological holdout. This is a reproducible sample result, not a claim about other datasets.

| Model | MAE | WAPE |
|---|---:|---:|
| Expanding mean | 1.5812 | 68.34% |
| Trailing-28-day mean | 1.4060 | 60.77% |
| Seasonal naive | 1.5935 | 68.87% |
| Weekday profile | 1.4217 | 61.45% |

The bundled run selects `trailing_28d_mean` because it has the lowest WAPE on this holdout. A different seed, scale, or holdout can select a different model; always use the metrics produced by the run being evaluated.
