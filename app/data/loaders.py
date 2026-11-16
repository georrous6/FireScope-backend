from collections.abc import Iterator
from pathlib import Path

import polars as pl

from app.core.config import settings


# Columns of the fire_detections table that come from the CSVs,
# in the order they are written to the database.
TABLE_COLUMNS: dict[str, pl.DataType] = {
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "brightness": pl.Float64,
    "bright_t31": pl.Float64,
    "bright_ti4": pl.Float64,
    "bright_ti5": pl.Float64,
    "scan": pl.Float64,
    "track": pl.Float64,
    "frp": pl.Float64,
    "acq_date": pl.Date,
    "acq_time": pl.Int32,
    "acq_datetime": pl.Datetime("us", "UTC"),
    "satellite": pl.String,
    "instrument": pl.String,
    "sensor": pl.String,
    "confidence": pl.String,
    "version": pl.String,
    "daynight": pl.String,
    "type": pl.Int32,
    "data_product": pl.String,
    "source_file": pl.String,
}

HARMONIZED_CELL_MONTH_COLUMNS: dict[str, pl.DataType] = {
    "month": pl.Date,
    "gx": pl.Int32,
    "gy": pl.Int32,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "source": pl.String,
    "detections": pl.Float64,
    "frp_mw": pl.Float64,
    "modis_converted_detections": pl.Float64,
    "modis_converted_frp_mw": pl.Float64,
    "viirs_detections": pl.Float64,
    "viirs_frp_mw": pl.Float64,
    "modis_raw_detections": pl.Float64,
    "modis_raw_frp_mw": pl.Float64,
}

HARMONIZED_NATIONAL_MONTH_COLUMNS: dict[str, pl.DataType] = {
    "month": pl.Date,
    "detections": pl.Float64,
    "frp_mw": pl.Float64,
    "modis_converted_detections": pl.Float64,
    "modis_converted_frp_mw": pl.Float64,
    "viirs_detections": pl.Float64,
    "viirs_frp_mw": pl.Float64,
    "modis_raw_detections": pl.Float64,
    "modis_raw_frp_mw": pl.Float64,
    "source": pl.String,
    "detections_p10": pl.Float64,
    "detections_p90": pl.Float64,
    "frp_mw_p10": pl.Float64,
    "frp_mw_p90": pl.Float64,
    "oos_modis_converted_detections": pl.Float64,
    "oos_modis_converted_frp_mw": pl.Float64,
    "oos_terra_only_converted_detections": pl.Float64,
}

# Columns that only exist for one sensor (brightness) or one product (type).
# Missing ones are added as NULL.
OPTIONAL_COLUMNS = [
    "brightness", "bright_t31", "bright_ti4", "bright_ti5", "type",
]

# Read these as text so Polars never guesses a number for them
# (e.g. MODIS confidence is 0-100, VIIRS is l/n/h; versions like "6.1NRT").
STRING_COLUMNS = {
    "acq_date": pl.String,
    "satellite": pl.String,
    "instrument": pl.String,
    "confidence": pl.String,
    "version": pl.String,
    "daynight": pl.String,
}

# The folder a file sits in decides its sensor. FIRMS is not consistent
# about the "instrument" column (e.g. VIIRS archives can say "SNPP"), so it
# is kept as-is for reference but not relied on.
SENSOR_FOLDERS = {"modis": "MODIS", "viirs": "VIIRS"}

# MODIS flies on Terra and Aqua; anything else is a VIIRS satellite
# (SNPP / N, NOAA-20 / N20 / 1, NOAA-21 / N21 / ...). Used to catch a file
# dropped into the wrong folder.
MODIS_SATELLITES = {"terra", "aqua", "t", "a"}

# Some VIIRS downloads use the MODIS column names. For VIIRS, the first
# brightness column is the I-4 channel and the second is I-5.
VIIRS_RENAMES = {"brightness": "bright_ti4", "bright_t31": "bright_ti5"}


class FIRMSDataLoader:
    """
    Reads NASA FIRMS CSV downloads and normalizes them to the
    fire_detections table layout.

    Expected layout (file names as FIRMS delivers them):

        <RAW_DATA_DIR>/modis/fire_archive_*.csv
        <RAW_DATA_DIR>/modis/fire_nrt_*.csv
        <RAW_DATA_DIR>/viirs/fire_archive_*.csv
        <RAW_DATA_DIR>/viirs/fire_nrt_*.csv
    """

    def __init__(self, raw_data_dir: Path | None = None):
        self.raw_data_dir = Path(raw_data_dir or settings.RAW_DATA_DIR)

    def find_files(self) -> list[tuple[Path, str, str]]:
        """Return (path, data_product, sensor) for every FIRMS CSV found."""

        files: list[tuple[Path, str, str]] = []

        for folder_name, sensor in SENSOR_FOLDERS.items():
            folder = self.raw_data_dir / folder_name
            for path in sorted(folder.glob("fire_archive_*.csv")):
                files.append((path, "archive", sensor))
            for path in sorted(folder.glob("fire_nrt_*.csv")):
                files.append((path, "nrt", sensor))

        return files

    @staticmethod
    def read_csv(path: Path) -> pl.DataFrame:
        return pl.read_csv(path, schema_overrides=STRING_COLUMNS)

    @staticmethod
    def normalize(
        df: pl.DataFrame,
        data_product: str,
        source_file: str,
        sensor: str,
    ) -> pl.DataFrame:
        """Convert one FIRMS CSV (MODIS or VIIRS) to the table layout."""

        satellites = {
            str(v).strip().lower()
            for v in df["satellite"].unique().drop_nulls().to_list()
        }
        looks_modis = bool(satellites) and satellites <= MODIS_SATELLITES
        if sensor == "MODIS" and not looks_modis:
            raise ValueError(
                f"{source_file} is in the modis/ folder but its satellites are "
                f"{sorted(satellites)}, not Terra/Aqua. Is it a VIIRS file? "
                "Move it to the viirs/ folder."
            )
        if sensor == "VIIRS" and satellites & MODIS_SATELLITES:
            raise ValueError(
                f"{source_file} is in the viirs/ folder but contains MODIS "
                f"satellites {sorted(satellites & MODIS_SATELLITES)}. "
                "Is it a MODIS file? Move it to the modis/ folder."
            )

        if sensor == "VIIRS" and "bright_ti4" not in df.columns:
            df = df.rename(
                {old: new for old, new in VIIRS_RENAMES.items() if old in df.columns}
            )

        missing = [c for c in OPTIONAL_COLUMNS if c not in df.columns]
        if missing:
            df = df.with_columns(
                pl.lit(None, dtype=TABLE_COLUMNS[c]).alias(c) for c in missing
            )

        df = df.with_columns(
            pl.lit(sensor).alias("sensor"),

            pl.concat_str(
                [
                    pl.col("acq_date"),
                    pl.lit(" "),
                    pl.col("acq_time").cast(pl.String).str.zfill(4),
                ]
            )
            .str.strptime(pl.Datetime("us"), format="%Y-%m-%d %H%M", strict=True)
            .dt.replace_time_zone("UTC")
            .alias("acq_datetime"),

            pl.col("acq_date").str.to_date("%Y-%m-%d"),

            pl.lit(data_product).alias("data_product"),
            pl.lit(source_file).alias("source_file"),
        )

        return df.select(
            pl.col(name).cast(dtype) for name, dtype in TABLE_COLUMNS.items()
        )

    def iter_files(self) -> Iterator[tuple[Path, pl.DataFrame]]:
        """Yield (path, normalized DataFrame) one file at a time."""

        for path, data_product, sensor in self.find_files():
            df = self.read_csv(path)
            yield path, self.normalize(df, data_product, path.name, sensor)

    def load_all(self) -> pl.DataFrame:
        """Load and combine every FIRMS CSV into one DataFrame."""

        frames = [df for _, df in self.iter_files()]

        if not frames:
            raise FileNotFoundError(
                f"No FIRMS CSVs found under {self.raw_data_dir}. "
                "Expected modis/ and viirs/ folders containing "
                "fire_archive_*.csv and/or fire_nrt_*.csv."
            )

        return pl.concat(frames, how="vertical")


class HarmonizedDataLoader:
    """
    Reads harmonized monthly CSV outputs produced by analysis/harmonize.py.

    Expected files:

        <HARMONIZED_DIR>/harmonized_cell_month.csv
        <HARMONIZED_DIR>/harmonized_national_month.csv
    """

    def __init__(self, harmonized_data_dir: Path | None = None):
        base = settings.RAW_DATA_DIR.parent
        self.harmonized_data_dir = Path(harmonized_data_dir or base / "harmonized")

    @property
    def cell_month_path(self) -> Path:
        return self.harmonized_data_dir / "harmonized_cell_month.csv"

    @property
    def national_month_path(self) -> Path:
        return self.harmonized_data_dir / "harmonized_national_month.csv"

    def find_files(self) -> dict[str, Path]:
        return {
            "cell": self.cell_month_path,
            "national": self.national_month_path,
        }

    @staticmethod
    def _normalize_month(df: pl.DataFrame) -> pl.DataFrame:
        # harmonize.py stores month as YYYY-MM, convert to month-start date.
        return df.with_columns(
            pl.col("month").str.strptime(pl.Date, format="%Y-%m", strict=True)
        )

    @staticmethod
    def _cast_to_schema(
        df: pl.DataFrame,
        schema: dict[str, pl.DataType],
        source_name: str,
    ) -> pl.DataFrame:
        missing = [c for c in schema if c not in df.columns]
        if missing:
            raise ValueError(
                f"{source_name} is missing required columns: {', '.join(missing)}"
            )

        return df.select(pl.col(name).cast(dtype) for name, dtype in schema.items())

    def load_cell_month(self) -> pl.DataFrame:
        path = self.cell_month_path
        if not path.exists():
            raise FileNotFoundError(f"Missing harmonized file: {path}")

        df = pl.read_csv(path)
        df = self._normalize_month(df)
        return self._cast_to_schema(
            df,
            HARMONIZED_CELL_MONTH_COLUMNS,
            path.name,
        )

    def load_national_month(self) -> pl.DataFrame:
        path = self.national_month_path
        if not path.exists():
            raise FileNotFoundError(f"Missing harmonized file: {path}")

        df = pl.read_csv(path)
        df = self._normalize_month(df)
        return self._cast_to_schema(
            df,
            HARMONIZED_NATIONAL_MONTH_COLUMNS,
            path.name,
        )
