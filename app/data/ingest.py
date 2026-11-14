"""
Load NASA FIRMS CSVs into the fire_detections table.

Usage (from the project root):

    python -m app.data.ingest              # load every CSV found
    python -m app.data.ingest --truncate   # empty the table first
    python -m app.data.ingest --dry-run    # read and validate only

Safe to re-run: each file's previous rows (matched by source_file) are
replaced in the same transaction, so loading a file twice never
duplicates detections.
"""

import argparse
import io
import sys
import time
from pathlib import Path

import polars as pl
from sqlalchemy import text

from app.core.database import engine
from app.data.loaders import TABLE_COLUMNS, FIRMSDataLoader

COPY_CHUNK_BYTES = 1 << 20  # 1 MiB


def to_csv_bytes(df: pl.DataFrame) -> bytes:
    buffer = io.BytesIO()
    df.write_csv(
        buffer,
        include_header=False,
        null_value="",
        datetime_format="%Y-%m-%d %H:%M:%S%z",
        date_format="%Y-%m-%d",
    )
    return buffer.getvalue()


def ingest_file(connection, df: pl.DataFrame, source_file: str) -> tuple[int, int]:
    """Replace one file's rows. Returns (rows_deleted, rows_inserted)."""

    columns = ", ".join(TABLE_COLUMNS)
    payload = to_csv_bytes(df)

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM fire_detections WHERE source_file = %s",
                (source_file,),
            )
            deleted = cursor.rowcount

            with cursor.copy(
                f"COPY fire_detections ({columns}) FROM STDIN WITH (FORMAT csv)"
            ) as copy:
                for start in range(0, len(payload), COPY_CHUNK_BYTES):
                    copy.write(payload[start:start + COPY_CHUNK_BYTES])

    return deleted, df.height


def check_table_exists() -> None:
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.fire_detections') IS NOT NULL")
        ).scalar()
    if not exists:
        sys.exit(
            "Table fire_detections does not exist. Create it first:\n"
            "  psql -h localhost -U firescope_user -d firescope_db "
            "-f database/schema.sql"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Folder with modis/ and viirs/ subfolders (default: RAW_DATA_DIR).",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete ALL rows from fire_detections before loading.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate the CSVs without writing to the database.",
    )
    args = parser.parse_args()

    loader = FIRMSDataLoader(args.data_dir)
    files = loader.find_files()

    if not files:
        sys.exit(
            f"No FIRMS CSVs found under {loader.raw_data_dir}.\n"
            "Put downloads in modis/ and viirs/ subfolders, named "
            "fire_archive_*.csv or fire_nrt_*.csv."
        )

    print(f"Found {len(files)} file(s) under {loader.raw_data_dir}")

    if args.dry_run:
        for path, df in loader.iter_files():
            print(f"  ok  {path.name}: {df.height:,} rows")
        print("Dry run: nothing written.")
        return

    check_table_exists()

    raw = engine.raw_connection()
    connection = raw.driver_connection  # the underlying psycopg connection
    total = 0
    started = time.perf_counter()

    try:
        if args.truncate:
            with connection.transaction():
                connection.execute("TRUNCATE fire_detections RESTART IDENTITY")
            print("  Table truncated.")

        for path, df in loader.iter_files():
            deleted, inserted = ingest_file(connection, df, path.name)
            total += inserted
            note = f" (replaced {deleted:,} old rows)" if deleted else ""
            print(f"  {path.name}: {inserted:,} rows{note}")

        with connection.transaction():
            connection.execute("ANALYZE fire_detections")
    finally:
        raw.close()

    elapsed = time.perf_counter() - started
    print(f"Done: {total:,} rows loaded in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()
