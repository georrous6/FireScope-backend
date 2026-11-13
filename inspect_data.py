import polars as pl
from app.data.loaders import FIRMSDataLoader


def main():
    loader = FIRMSDataLoader()

    df = loader.load_all()

    print(f"Combined shape: {df.shape}")
    print("\nSchema:")
    print(df.schema)

    print("\nDetections by instrument and data product:")
    print(
        df.group_by("instrument", "satellite", "data_product")
        .len()
        .sort(["instrument", "satellite", "data_product"])
    )

    print("\nTemporal coverage:")
    print(
        df.select(
            pl.col("acq_datetime").min().alias("start"),
            pl.col("acq_datetime").max().alias("end"),
        )
    )

    print("\nSample:")
    print(df.head(10))


if __name__ == "__main__":
    main()