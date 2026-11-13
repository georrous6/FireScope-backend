import polars as pl

from app.core.config import settings


class FIRMSDataLoader:

    def __init__(self):
        self.modis_dir = settings.raw_data_dir / "modis"
        self.viirs_dir = settings.raw_data_dir / "viirs"

    def load_modis_archive(self) -> pl.DataFrame:
        path = self.modis_dir / "fire_archive_M-C61_844885.csv"
        return pl.read_csv(path)

    def load_modis_nrt(self) -> pl.DataFrame:
        path = self.modis_dir / "fire_nrt_M-C61_844885.csv"
        return pl.read_csv(path)

    def load_viirs_archive(self) -> pl.DataFrame:
        path = self.viirs_dir / "fire_archive_SV-C2_844905.csv"
        return pl.read_csv(path)

    def load_viirs_nrt(self) -> pl.DataFrame:
        path = self.viirs_dir / "fire_nrt_SV-C2_844905.csv"
        return pl.read_csv(path)

    @staticmethod
    def normalize_modis(
        df: pl.DataFrame,
        data_product: str,
    ) -> pl.DataFrame:

        if "type" not in df.columns:
            df = df.with_columns(
                pl.lit(None, dtype=pl.Int64).alias("type")
            )

        return df.with_columns(
            pl.col("version").cast(pl.String),

            pl.concat_str(
                [
                    pl.col("acq_date").cast(pl.String),
                    pl.lit(" "),
                    pl.col("acq_time").cast(pl.String).str.zfill(4),
                ]
            )
            .str.strptime(pl.Datetime, format="%Y-%m-%d %H%M", strict=True)
            .dt.replace_time_zone("UTC")
            .alias("acq_datetime"),

            pl.lit(data_product).alias("data_product"),
        )

    @staticmethod
    def normalize_viirs(
        df: pl.DataFrame,
        data_product: str,
    ) -> pl.DataFrame:

        if "type" not in df.columns:
            df = df.with_columns(
                pl.lit(None, dtype=pl.Int64).alias("type")
            )

        return df.with_columns(
            pl.col("version").cast(pl.String),

            pl.concat_str(
                [
                    pl.col("acq_date").cast(pl.String),
                    pl.lit(" "),
                    pl.col("acq_time").cast(pl.String).str.zfill(4),
                ]
            )
            .str.strptime(pl.Datetime, format="%Y-%m-%d %H%M", strict=True)
            .dt.replace_time_zone("UTC")
            .alias("acq_datetime"),

            pl.lit(data_product).alias("data_product"),
        )

    def load_all(self) -> pl.DataFrame:
        datasets = [
            self.normalize_modis(
                self.load_modis_archive(),
                data_product="archive",
            ),
            self.normalize_modis(
                self.load_modis_nrt(),
                data_product="nrt",
            ),
            self.normalize_viirs(
                self.load_viirs_archive(),
                data_product="archive",
            ),
            self.normalize_viirs(
                self.load_viirs_nrt(),
                data_product="nrt",
            ),
        ]

        return pl.concat(
            datasets,
            how="diagonal_relaxed",
        )