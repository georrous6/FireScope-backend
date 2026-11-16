from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Annotated

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field


class Sensor(str, Enum):
    MODIS = "MODIS"
    VIIRS = "VIIRS"


class DataProduct(str, Enum):
    archive = "archive"
    nrt = "nrt"


class DayNight(str, Enum):
    D = "D"
    N = "N"


class Interval(str, Enum):
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class ForecastMetric(str, Enum):
    detections = "detections"
    frp = "frp"


@dataclass
class BBox:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


def parse_bbox(value: str | None) -> BBox | None:
    if value is None:
        return None

    try:
        parts = [float(p) for p in value.split(",")]
    except ValueError:
        parts = []

    if len(parts) != 4:
        raise HTTPException(
            422, "bbox must be 4 numbers: min_lon,min_lat,max_lon,max_lat"
        )

    box = BBox(*parts)

    if not (-180 <= box.min_lon < box.max_lon <= 180):
        raise HTTPException(
            422, "bbox longitudes must satisfy -180 <= min_lon < max_lon <= 180"
        )

    if not (-90 <= box.min_lat < box.max_lat <= 90):
        raise HTTPException(
            422, "bbox latitudes must satisfy -90 <= min_lat < max_lat <= 90"
        )

    return box


@dataclass
class DetectionFilters:
    """Filters shared by every /detections endpoint."""

    bbox: BBox | None
    start_date: date | None
    end_date: date | None
    sensors: list[Sensor] | None
    data_products: list[DataProduct] | None
    daynight: DayNight | None
    min_frp: float | None


def detection_filters(
    bbox: Annotated[
        str | None,
        Query(
            description="Map area as min_lon,min_lat,max_lon,max_lat (WGS84).",
            examples=["19.3,34.7,29.7,41.8"],
        ),
    ] = None,
    start_date: Annotated[
        date | None,
        Query(description="First day to include (UTC), e.g. 2025-07-01."),
    ] = None,
    end_date: Annotated[
        date | None,
        Query(description="Last day to include (UTC), inclusive."),
    ] = None,
    sensor: Annotated[
        list[Sensor] | None,
        Query(description="MODIS and/or VIIRS. Repeat to pass both; omit for all."),
    ] = None,
    data_product: Annotated[
        list[DataProduct] | None,
        Query(description="archive and/or nrt. Omit for both."),
    ] = None,
    daynight: Annotated[
        DayNight | None,
        Query(description="D for daytime or N for night-time detections."),
    ] = None,
    min_frp: Annotated[
        float | None,
        Query(ge=0, description="Minimum fire radiative power (MW)."),
    ] = None,
) -> DetectionFilters:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(422, "start_date must be on or before end_date")

    return DetectionFilters(
        bbox=parse_bbox(bbox),
        start_date=start_date,
        end_date=end_date,
        sensors=sensor or None,
        data_products=data_product or None,
        daynight=daynight,
        min_frp=min_frp,
    )


class TimeSeriesPoint(BaseModel):
    period: date = Field(description="Start of the day/week/month/year (UTC).")
    sensor: Sensor
    count: int
    total_frp: float | None = Field(description="Sum of fire radiative power (MW).")


class SensorCoverage(BaseModel):
    sensor: Sensor
    data_product: DataProduct
    count: int
    first_date: date
    last_date: date


class Coverage(BaseModel):
    total: int
    bbox: list[float] | None = Field(
        description="Extent of all detections: [min_lon, min_lat, max_lon, max_lat]."
    )
    first_date: date | None
    last_date: date | None
    by_source: list[SensorCoverage]


class HarmonizedCellDetectionsPoint(BaseModel):
    month: date = Field(description="Month bucket (UTC), stored as month start date.")
    detections: float
    source: str


class HarmonizedCellFrpPoint(BaseModel):
    month: date = Field(description="Month bucket (UTC), stored as month start date.")
    frp_mw: float
    source: str


class HarmonizedCellDetectionsSeries(BaseModel):
    gx: int
    gy: int
    lat: float = Field(description="Grid-cell center latitude.")
    lon: float = Field(description="Grid-cell center longitude.")
    points: list[HarmonizedCellDetectionsPoint]


class HarmonizedCellFrpSeries(BaseModel):
    gx: int
    gy: int
    lat: float = Field(description="Grid-cell center latitude.")
    lon: float = Field(description="Grid-cell center longitude.")
    points: list[HarmonizedCellFrpPoint]


class ForecastPoint(BaseModel):
    month: date = Field(description="Forecast month, stored as month start date.")
    value: float
    lower_bound: float | None = Field(
        default=None,
        description="Lower prediction interval bound, when available.",
    )
    upper_bound: float | None = Field(
        default=None,
        description="Upper prediction interval bound, when available.",
    )


class ForecastHistoricalPoint(BaseModel):
    month: date
    value: float


class HarmonizedCellForecast(BaseModel):
    gx: int
    gy: int
    lat: float = Field(description="Grid-cell center latitude.")
    lon: float = Field(description="Grid-cell center longitude.")

    metric: ForecastMetric
    unit: str
    horizon: int

    model: str = Field(description="Forecasting model used.")
    historical_points: int
    sources: list[str]

    historical_data: list[ForecastHistoricalPoint]
    forecast: list[ForecastPoint]