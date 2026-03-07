"""Deterministic spend forecasting with simple linear regression."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class ForecastPoint:
    """One month-ahead forecast point with confidence bounds."""

    month_offset: int
    predicted_spend_usd: float
    lower_bound_usd: float
    upper_bound_usd: float


@dataclass(frozen=True)
class SpendForecast:
    """Forecast response envelope."""

    trend_pct: float
    residual_stddev_usd: float
    points: list[ForecastPoint]


class SpendForecastEngine:
    """Pure in-memory forecast engine for dashboard and planning APIs."""

    def forecast(self, monthly_spend_usd: list[float], horizon_months: int = 3) -> SpendForecast:
        """Forecast monthly spend using least-squares trend + residual envelope."""
        if len(monthly_spend_usd) < 2:
            msg = "at least two months of spend history are required"
            raise ValueError(msg)
        if horizon_months <= 0:
            msg = "horizon_months must be > 0"
            raise ValueError(msg)

        x_vals = [float(idx) for idx in range(len(monthly_spend_usd))]
        y_vals = [float(value) for value in monthly_spend_usd]

        n = float(len(x_vals))
        x_mean = sum(x_vals) / n
        y_mean = sum(y_vals) / n

        denom = sum((x - x_mean) ** 2 for x in x_vals)
        if denom == 0:
            slope = 0.0
        else:
            slope = (
                sum(
                    (x - x_mean) * (y - y_mean)
                    for x, y in zip(x_vals, y_vals, strict=True)
                )
                / denom
            )
        intercept = y_mean - (slope * x_mean)

        residuals = [
            y - (intercept + slope * x) for x, y in zip(x_vals, y_vals, strict=True)
        ]
        residual_variance = sum(r * r for r in residuals) / n
        residual_stddev = sqrt(residual_variance)
        confidence_width = 1.96 * residual_stddev

        points: list[ForecastPoint] = []
        start_idx = len(monthly_spend_usd)
        for offset in range(1, horizon_months + 1):
            month_index = float(start_idx + (offset - 1))
            prediction = max(intercept + (slope * month_index), 0.0)
            lower = max(prediction - confidence_width, 0.0)
            upper = max(prediction + confidence_width, lower)
            points.append(
                ForecastPoint(
                    month_offset=offset,
                    predicted_spend_usd=prediction,
                    lower_bound_usd=lower,
                    upper_bound_usd=upper,
                )
            )

        baseline = y_vals[0]
        latest = y_vals[-1]
        trend_pct = 0.0 if baseline == 0 else ((latest - baseline) / baseline) * 100.0

        return SpendForecast(
            trend_pct=trend_pct,
            residual_stddev_usd=residual_stddev,
            points=points,
        )
