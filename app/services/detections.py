"""
Database queries and forecasting services for fire detections.

GeoJSON is assembled inside PostgreSQL and passed through as text: that is
much faster than turning tens of thousands of rows into Python objects and
back into JSON.
"""

from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.schemas.detections import (
    Coverage,
    DetectionFilters,
    ForecastHistoricalPoint,
    ForecastMetric,
    ForecastPoint,
    HarmonizedCellDetectionsPoint,
    HarmonizedCellDetectionsSeries,
    HarmonizedCellForecast,
    HarmonizedCellFrpPoint,
    HarmonizedCellFrpSeries,
    Interval,
    SensorCoverage,
    TimeSeriesPoint,
)


HARMONIZED_GRID_DEGREES = 0.25

# SARIMA configuration for monthly fire-activity series.
SARIMA_ORDER = (1, 0, 0)
SARIMA_SEASONAL_ORDER = (1, 1, 0, 12)

# Require at least three complete annual cycles before fitting SARIMA.
MIN_SARIMA_OBSERVATIONS = 36


def build_where(f: DetectionFilters) -> tuple[str, dict[str, Any]]:
    """Turn filters into a SQL WHERE clause with bound parameters."""

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if f.bbox:
        # && uses the spatial (GiST) index.
        conditions.append(
            "geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)"
        )
        params.update(
            min_lon=f.bbox.min_lon,
            min_lat=f.bbox.min_lat,
            max_lon=f.bbox.max_lon,
            max_lat=f.bbox.max_lat,
        )

    # Dates are whole UTC days; filtering on acq_datetime uses its index.
    if f.start_date:
        conditions.append("acq_datetime >= :start_ts")
        params["start_ts"] = datetime.combine(f.start_date, time.min, timezone.utc)

    if f.end_date:
        conditions.append("acq_datetime < :end_ts")
        params["end_ts"] = datetime.combine(
            f.end_date + timedelta(days=1), time.min, timezone.utc
        )

    if f.sensors:
        conditions.append("sensor = ANY(:sensors)")
        params["sensors"] = [s.value for s in f.sensors]

    if f.data_products:
        conditions.append("data_product = ANY(:data_products)")
        params["data_products"] = [p.value for p in f.data_products]

    if f.daynight:
        conditions.append("daynight = :daynight")
        params["daynight"] = f.daynight.value

    if f.min_frp is not None:
        conditions.append("frp >= :min_frp")
        params["min_frp"] = f.min_frp

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def detections_geojson(
    conn: Connection, f: DetectionFilters, limit: int
) -> str:
    """Individual detections as a GeoJSON FeatureCollection (newest first)."""

    where, params = build_where(f)
    params["limit"] = limit

    sql = text(f"""
        WITH picked AS (
            SELECT *
            FROM fire_detections
            {where}
            ORDER BY acq_datetime DESC
            LIMIT :limit + 1
        ),
        shown AS (
            SELECT * FROM picked ORDER BY acq_datetime DESC LIMIT :limit
        )
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'count', (SELECT count(*) FROM shown),
            'truncated', (SELECT count(*) FROM picked) > :limit,
            'features', COALESCE(
                (SELECT json_agg(json_build_object(
                    'type', 'Feature',
                    'id', id,
                    'geometry', ST_AsGeoJSON(geom, 6)::json,
                    'properties', json_build_object(
                        'acq_datetime', acq_datetime,
                        'sensor', sensor,
                        'satellite', satellite,
                        'data_product', data_product,
                        'frp', frp,
                        'confidence', confidence,
                        'daynight', daynight,
                        'brightness', COALESCE(brightness, bright_ti4),
                        'bright_secondary', COALESCE(bright_t31, bright_ti5),
                        'scan', scan,
                        'track', track,
                        'type', type
                    )
                ) ORDER BY acq_datetime DESC) FROM shown),
                '[]'::json
            )
        )::text
    """)
    return conn.execute(sql, params).scalar_one()


def grid_geojson(
    conn: Connection, f: DetectionFilters, cell_size: float
) -> str:
    """
    Detections aggregated into square grid cells, as GeoJSON polygons.
    Good for zoomed-out maps and heatmaps, where individual points would
    be too many to draw.
    """

    where, params = build_where(f)
    params["cell"] = cell_size

    sql = text(f"""
        WITH cells AS (
            SELECT
                floor(longitude / :cell) AS gx,
                floor(latitude / :cell) AS gy,
                count(*) AS n,
                sum(frp) AS total_frp,
                max(frp) AS max_frp,
                count(*) FILTER (WHERE sensor = 'MODIS') AS modis,
                count(*) FILTER (WHERE sensor = 'VIIRS') AS viirs,
                min(acq_datetime) AS first_seen,
                max(acq_datetime) AS last_seen
            FROM fire_detections
            {where}
            GROUP BY 1, 2
        )
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'cell_size', CAST(:cell AS double precision),
            'count', (SELECT count(*) FROM cells),
            'max_count', (SELECT max(n) FROM cells),
            'features', COALESCE(
                (SELECT json_agg(json_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(ST_MakeEnvelope(
                        gx * :cell, gy * :cell,
                        (gx + 1) * :cell, (gy + 1) * :cell, 4326
                    ), 6)::json,
                    'properties', json_build_object(
                        'count', n,
                        'total_frp', round(total_frp::numeric, 2),
                        'max_frp', max_frp,
                        'modis', modis,
                        'viirs', viirs,
                        'first_seen', first_seen,
                        'last_seen', last_seen,
                        'center', json_build_array(
                            round(((gx + 0.5) * :cell)::numeric, 6),
                            round(((gy + 0.5) * :cell)::numeric, 6)
                        )
                    )
                ) ORDER BY n DESC) FROM cells),
                '[]'::json
            )
        )::text
    """)
    return conn.execute(sql, params).scalar_one()


def time_series(
    conn: Connection, f: DetectionFilters, interval: Interval
) -> list[TimeSeriesPoint]:
    """Detection counts and total FRP per period and sensor, for charts."""

    where, params = build_where(f)

    # interval is a validated enum, so it is safe to put in the SQL text.
    sql = text(f"""
        SELECT
            date_trunc('{interval.value}', acq_datetime AT TIME ZONE 'UTC')::date AS period,
            sensor,
            count(*) AS count,
            round(sum(frp)::numeric, 2)::float8 AS total_frp
        FROM fire_detections
        {where}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """)

    rows = conn.execute(sql, params).mappings().all()
    return [TimeSeriesPoint(**row) for row in rows]


def coverage(conn: Connection) -> Coverage:
    """What data is loaded: counts, date ranges and extent."""

    by_source = conn.execute(text("""
        SELECT sensor, data_product, count(*) AS count,
               min(acq_date) AS first_date, max(acq_date) AS last_date
        FROM fire_detections
        GROUP BY 1, 2
        ORDER BY 1, 2
    """)).mappings().all()

    extent = conn.execute(text("""
        SELECT min(longitude), min(latitude), max(longitude), max(latitude)
        FROM fire_detections
    """)).one()

    sources = [SensorCoverage(**row) for row in by_source]

    return Coverage(
        total=sum(s.count for s in sources),
        bbox=list(extent) if extent[0] is not None else None,
        first_date=min((s.first_date for s in sources), default=None),
        last_date=max((s.last_date for s in sources), default=None),
        by_source=sources,
    )


def _grid_cell_for_coordinates(lat: float, lon: float) -> tuple[int, int]:
    return (
        math.floor(lon / HARMONIZED_GRID_DEGREES),
        math.floor(lat / HARMONIZED_GRID_DEGREES),
    )


def _cell_exists(conn: Connection, gx: int, gy: int) -> bool:
    exists = conn.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM harmonized_cell_month
                WHERE gx = :gx AND gy = :gy
            )
        """),
        {"gx": gx, "gy": gy},
    ).scalar_one()

    return bool(exists)


def _cell_series_rows(
    conn: Connection,
    gx: int,
    gy: int,
    value_column: str,
) -> list[dict[str, Any]]:
    """
    Retrieve a continuous monthly series for a harmonized grid cell.

    The national-month table supplies the complete set of months. Months
    without a cell observation are represented as zero, consistent with
    the existing harmonized-cell endpoints.
    """

    # value_column is selected internally from a fixed set of column names.
    if value_column not in {"detections", "frp_mw"}:
        raise ValueError(f"Unsupported harmonized value column: {value_column}")

    sql = text(f"""
        WITH target_cell AS (
            SELECT
                CAST(:gx AS INTEGER) AS gx,
                CAST(:gy AS INTEGER) AS gy,
                ROUND(
                    ((CAST(:gy AS INTEGER) + 0.5) * :grid)::numeric,
                    6
                )::float8 AS lat,
                ROUND(
                    ((CAST(:gx AS INTEGER) + 0.5) * :grid)::numeric,
                    6
                )::float8 AS lon
        )
        SELECT
            t.gx,
            t.gy,
            t.lat,
            t.lon,
            n.month,
            COALESCE(c.{value_column}, 0)::float8 AS value,
            COALESCE(c.source, n.source) AS source
        FROM target_cell t
        JOIN harmonized_national_month n ON true
        LEFT JOIN harmonized_cell_month c
            ON c.month = n.month
           AND c.gx = t.gx
           AND c.gy = t.gy
        ORDER BY n.month
    """)

    return conn.execute(
        sql,
        {
            "gx": gx,
            "gy": gy,
            "grid": HARMONIZED_GRID_DEGREES,
        },
    ).mappings().all()


def harmonized_cell_detections_series(
    conn: Connection,
    lat: float,
    lon: float,
) -> HarmonizedCellDetectionsSeries | None:
    gx, gy = _grid_cell_for_coordinates(lat, lon)

    if not _cell_exists(conn, gx, gy):
        return None

    rows = _cell_series_rows(conn, gx, gy, "detections")

    if not rows:
        return None

    return HarmonizedCellDetectionsSeries(
        gx=rows[0]["gx"],
        gy=rows[0]["gy"],
        lat=rows[0]["lat"],
        lon=rows[0]["lon"],
        points=[
            HarmonizedCellDetectionsPoint(
                month=row["month"],
                detections=row["value"],
                source=row["source"],
            )
            for row in rows
        ],
    )


def harmonized_cell_frp_series(
    conn: Connection,
    lat: float,
    lon: float,
) -> HarmonizedCellFrpSeries | None:
    gx, gy = _grid_cell_for_coordinates(lat, lon)

    if not _cell_exists(conn, gx, gy):
        return None

    rows = _cell_series_rows(conn, gx, gy, "frp_mw")

    if not rows:
        return None

    return HarmonizedCellFrpSeries(
        gx=rows[0]["gx"],
        gy=rows[0]["gy"],
        lat=rows[0]["lat"],
        lon=rows[0]["lon"],
        points=[
            HarmonizedCellFrpPoint(
                month=row["month"],
                frp_mw=row["value"],
                source=row["source"],
            )
            for row in rows
        ],
    )


def _next_month(month: date) -> date:
    """Return the first day of the month following the supplied month."""

    if month.month == 12:
        return date(month.year + 1, 1, 1)

    return date(month.year, month.month + 1, 1)


def _forecast_months(last_month: date, horizon: int) -> list[date]:
    months: list[date] = []
    current = last_month

    for _ in range(horizon):
        current = _next_month(current)
        months.append(current)

    return months


def _seasonal_naive_forecast(
    values: list[float],
    horizon: int,
) -> list[float]:
    """
    Seasonal-naive forecast.

    With at least 12 observations, repeat the corresponding month from
    the previous year. Otherwise, repeat the latest observation.
    """

    if len(values) >= 12:
        seasonal_values = values[-12:]
        return [
            float(seasonal_values[i % 12])
            for i in range(horizon)
        ]

    last_value = float(values[-1]) if values else 0.0
    return [last_value] * horizon


def _fit_sarima_forecast(
    values: list[float],
    horizon: int,
) -> tuple[list[float], list[float], list[float]]:
    """
    Fit SARIMA and return point forecasts and prediction interval bounds.

    Raises an exception if statsmodels is unavailable or model fitting fails.
    The caller is responsible for selecting a fallback.
    """

    import pandas as pd
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    series = pd.Series(
        values,
        index=pd.date_range(
            start="2000-01-01",
            periods=len(values),
            freq="MS",
        ),
        dtype="float64",
    )

    model = SARIMAX(
        series,
        order=SARIMA_ORDER,
        seasonal_order=SARIMA_SEASONAL_ORDER,
        enforce_stationarity=False,
        enforce_invertibility=False,
    )

    fitted = model.fit(disp=False, maxiter=50)
    prediction = fitted.get_forecast(steps=horizon)

    predicted_values = prediction.predicted_mean.tolist()
    confidence_intervals = prediction.conf_int(alpha=0.05)

    lower_bounds = confidence_intervals.iloc[:, 0].tolist()
    upper_bounds = confidence_intervals.iloc[:, 1].tolist()

    # Fire detections and FRP cannot be negative.
    predicted_values = [max(0.0, float(v)) for v in predicted_values]
    lower_bounds = [max(0.0, float(v)) for v in lower_bounds]
    upper_bounds = [max(0.0, float(v)) for v in upper_bounds]

    return predicted_values, lower_bounds, upper_bounds


def forecast_harmonized_cell(
    conn: Connection,
    lat: float,
    lon: float,
    metric: ForecastMetric,
    horizon: int,
) -> HarmonizedCellForecast | None:
    """
    Forecast harmonized monthly fire activity for the cell containing
    the supplied coordinates.

    SARIMA is used when sufficient history is available. Otherwise,
    a seasonal-naive forecast is returned.
    """

    gx, gy = _grid_cell_for_coordinates(lat, lon)

    if not _cell_exists(conn, gx, gy):
        return None

    if metric == ForecastMetric.detections:
        value_column = "detections"
        unit = "detections"
    else:
        value_column = "frp_mw"
        unit = "MW"

    rows = _cell_series_rows(conn, gx, gy, value_column)

    if not rows:
        return None

    historical_values = [float(row["value"]) for row in rows]
    historical_data = [
        ForecastHistoricalPoint(
            month=row["month"],
            value=float(row["value"]),
        )
        for row in rows
    ]

    last_month = rows[-1]["month"]
    forecast_dates = _forecast_months(last_month, horizon)

    sources = sorted({
        str(row["source"])
        for row in rows
        if row["source"] is not None
    })

    lower_bounds: list[float] | None = None
    upper_bounds: list[float] | None = None

    # An entirely zero-valued series has a straightforward zero forecast.
    if not any(value != 0 for value in historical_values):
        predicted_values = [0.0] * horizon
        model_name = "zero_baseline"

    elif len(historical_values) >= MIN_SARIMA_OBSERVATIONS:
        try:
            (
                predicted_values,
                lower_bounds,
                upper_bounds,
            ) = _fit_sarima_forecast(historical_values, horizon)

            model_name = (
                f"SARIMA{SARIMA_ORDER}"
                f"x{SARIMA_SEASONAL_ORDER}"
            )

        except Exception:
            # A short, sparse or otherwise unsuitable series should not
            # make the endpoint fail. Use a deterministic fallback.
            predicted_values = _seasonal_naive_forecast(
                historical_values,
                horizon,
            )
            model_name = "seasonal_naive"

    else:
        predicted_values = _seasonal_naive_forecast(
            historical_values,
            horizon,
        )
        model_name = "seasonal_naive"

    forecast_points = []

    for i, (forecast_date, value) in enumerate(
        zip(forecast_dates, predicted_values)
    ):
        forecast_points.append(
            ForecastPoint(
                month=forecast_date,
                value=float(value),
                lower_bound=(
                    float(lower_bounds[i])
                    if lower_bounds is not None
                    else None
                ),
                upper_bound=(
                    float(upper_bounds[i])
                    if upper_bounds is not None
                    else None
                ),
            )
        )

    return HarmonizedCellForecast(
        gx=rows[0]["gx"],
        gy=rows[0]["gy"],
        lat=rows[0]["lat"],
        lon=rows[0]["lon"],
        metric=metric,
        unit=unit,
        horizon=horizon,
        model=model_name,
        historical_points=len(historical_values),
        sources=sources,
        historical_data=historical_data,
        forecast=forecast_points,
    )