"""Shared, testable business rules used by production pipelines and ground-truth fixtures."""
import pandas as pd


def is_demand_observation_usable(demand_censored_flag, stockout_flag):
    """Return false for sales observations whose true demand is censored by a stockout."""
    return not bool(int(demand_censored_flag) or int(stockout_flag))


def usable_demand_mask(frame):
    """Vectorized version of the same stockout/censoring rule for CSV chunks."""
    return (frame["demand_censored_flag"] == 0) & (frame["stockout_flag"] == 0)


def is_feature_allowed(feature_sale_date, forecast_origin_date):
    """Features must not contain observations later than the forecast origin."""
    return str(feature_sale_date) <= str(forecast_origin_date)


def feature_dates_allowed(sale_dates, forecast_origin_date):
    """Vectorized date-cutoff rule used to build forecasting training data."""
    return sale_dates <= pd.Timestamp(forecast_origin_date)
