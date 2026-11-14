from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.engine import Connection

from app.core.database import get_connection
from app.schemas.detections import (
    Coverage,
    DetectionFilters,
    Interval,
    TimeSeriesPoint,
    detection_filters,
)
from app.services import detections as service

router = APIRouter(prefix="/detections", tags=["Detections"])

Conn = Annotated[Connection, Depends(get_connection)]
Filters = Annotated[DetectionFilters, Depends(detection_filters)]

GEOJSON = "application/geo+json"

GEOJSON_RESPONSE = {
    200: {
        "description": "GeoJSON FeatureCollection.",
        "content": {GEOJSON: {"schema": {"type": "object"}}},
    }
}


@router.get(
    "",
    summary="Fire detections as GeoJSON points",
    response_class=Response,
    responses=GEOJSON_RESPONSE,
)
def list_detections(
    conn: Conn,
    filters: Filters,
    limit: Annotated[
        int,
        Query(ge=1, le=50_000, description="Maximum number of points returned."),
    ] = 5_000,
) -> Response:
    """
    Individual hotspots inside the filters, newest first, ready to drop onto
    a Leaflet / MapLibre / OpenLayers map.

    `truncated: true` in the response means more detections matched than
    `limit`: zoom in, narrow the dates, or use `/detections/grid` instead.

    `brightness` is the main brightness temperature (MODIS band 21/22 or
    VIIRS I-4) and `bright_secondary` the second one (MODIS band 31 or
    VIIRS I-5), so both sensors can share one colour scale in the frontend.
    """
    body = service.detections_geojson(conn, filters, limit)
    return Response(content=body, media_type=GEOJSON)


@router.get(
    "/grid",
    summary="Hotspots aggregated into grid cells (GeoJSON polygons)",
    response_class=Response,
    responses=GEOJSON_RESPONSE,
)
def detections_grid(
    conn: Conn,
    filters: Filters,
    cell_size: Annotated[
        float,
        Query(
            ge=0.01,
            le=5,
            description="Cell size in degrees (0.1 is about 11 km north-south).",
        ),
    ] = 0.1,
) -> Response:
    """
    Counts, total and max FRP per square cell. Use this for zoomed-out
    views, heatmaps and "where are the hot spots" questions, where drawing
    every point would be too slow. `max_count` helps scale the colours.
    """
    body = service.grid_geojson(conn, filters, cell_size)
    return Response(content=body, media_type=GEOJSON)


@router.get(
    "/timeseries",
    summary="Detection counts over time, per sensor",
    response_model=list[TimeSeriesPoint],
)
def detections_timeseries(
    conn: Conn,
    filters: Filters,
    interval: Annotated[
        Interval, Query(description="Bucket size for the time axis.")
    ] = Interval.day,
) -> list[TimeSeriesPoint]:
    """Number of detections and total FRP per day/week/month/year, for charts."""
    return service.time_series(conn, filters, interval)


@router.get(
    "/coverage",
    summary="What data is loaded",
    response_model=Coverage,
)
def detections_coverage(conn: Conn) -> Coverage:
    """
    Totals, first/last dates and map extent of the loaded data. Useful for
    setting the frontend's date picker limits and initial map view.
    """
    return service.coverage(conn)
