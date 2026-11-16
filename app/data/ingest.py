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
from app.data.loaders import (
    HARMONIZED_CELL_MONTH_COLUMNS,
    HARMONIZED_NATIONAL_MONTH_COLUMNS,
    TABLE_COLUMNS,
    FIRMSDataLoader,
    HarmonizedDataLoader,
)

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


def replace_table(connection, table_name: str, df: pl.DataFrame, columns_map: dict[str, pl.DataType]) -> tuple[int, int]:
    """Replace all rows in one table. Returns (rows_deleted, rows_inserted)."""

    columns = ", ".join(columns_map)
    payload = to_csv_bytes(df)

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            deleted = cursor.fetchone()[0]

            cursor.execute(f"TRUNCATE {table_name} RESTART IDENTITY")

            with cursor.copy(
                f"COPY {table_name} ({columns}) FROM STDIN WITH (FORMAT csv)"
            ) as copy:
                for start in range(0, len(payload), COPY_CHUNK_BYTES):
                    copy.write(payload[start:start + COPY_CHUNK_BYTES])

    return deleted, df.height


def check_tables_exist(table_names: list[str]) -> None:
    missing: list[str] = []

    with engine.connect() as conn:
        for table_name in table_names:
            exists = conn.execute(
                text("SELECT to_regclass(:table_name) IS NOT NULL"),
                {"table_name": f"public.{table_name}"},
            ).scalar()
            if not exists:
                missing.append(table_name)

    if missing:
        missing_list = ", ".join(missing)
        sys.exit(
            f"Missing required table(s): {missing_list}. Create them first:\n"
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
    parser.add_argument(
        "--with-harmonized",
        action="store_true",
        help="Also load data/harmonized/harmonized_*.csv into monthly summary tables.",
    )
    parser.add_argument(
        "--harmonized-only",
        action="store_true",
        help="Load only harmonized monthly CSVs and skip FIRMS raw files.",
    )
    parser.add_argument(
        "--harmonized-dir",
        type=Path,
        help="Folder with harmonized_cell_month.csv and harmonized_national_month.csv.",
    )
    args = parser.parse_args()

    load_harmonized = args.with_harmonized or args.harmonized_only
    load_firms = not args.harmonized_only

    if load_firms:
        loader = FIRMSDataLoader(args.data_dir)
        files = loader.find_files()
    else:
        loader = None
        files = []

    if load_harmonized:
        harmonized_loader = HarmonizedDataLoader(args.harmonized_dir)
    else:
        harmonized_loader = None

    if load_firms and not files and not load_harmonized:
        sys.exit(
            f"No FIRMS CSVs found under {loader.raw_data_dir}.\n"
            "Put downloads in modis/ and viirs/ subfolders, named "
            "fire_archive_*.csv or fire_nrt_*.csv."
        )

    if load_firms:
        if files:
            print(f"Found {len(files)} FIRMS file(s) under {loader.raw_data_dir}")
        else:
            print(f"No FIRMS CSVs found under {loader.raw_data_dir}; skipping raw ingestion.")

    if load_harmonized:
        assert harmonized_loader is not None
        paths = harmonized_loader.find_files()
        print(f"Harmonized source folder: {harmonized_loader.harmonized_data_dir}")
        for key, path in paths.items():
            status = "ok" if path.exists() else "missing"
            print(f"  {key}: {path.name} [{status}]")

    if args.dry_run:
        if load_firms:
            assert loader is not None
            for path, df in loader.iter_files():
                print(f"  ok  {path.name}: {df.height:,} rows")

        if load_harmonized:
            assert harmonized_loader is not None
            cell_df = harmonized_loader.load_cell_month()
            nat_df = harmonized_loader.load_national_month()
            print(f"  ok  {harmonized_loader.cell_month_path.name}: {cell_df.height:,} rows")
            print(f"  ok  {harmonized_loader.national_month_path.name}: {nat_df.height:,} rows")

        print("Dry run: nothing written.")
        return

    required_tables: list[str] = []
    if load_firms:
        required_tables.append("fire_detections")
    if load_harmonized:
        required_tables.extend(["harmonized_cell_month", "harmonized_national_month"])
    check_tables_exist(required_tables)

    raw = engine.raw_connection()
    connection = raw.driver_connection  # the underlying psycopg connection
    total = 0
    started = time.perf_counter()
    analyzed_tables: list[str] = []

    try:
        if args.truncate and load_firms:
            with connection.transaction():
                connection.execute("TRUNCATE fire_detections RESTART IDENTITY")
            print("  Table truncated.")

        if load_firms:
            assert loader is not None
            for path, df in loader.iter_files():
                deleted, inserted = ingest_file(connection, df, path.name)
                total += inserted
                note = f" (replaced {deleted:,} old rows)" if deleted else ""
                print(f"  {path.name}: {inserted:,} rows{note}")
            analyzed_tables.append("fire_detections")

        if load_harmonized:
            assert harmonized_loader is not None
            cell_df = harmonized_loader.load_cell_month()
            nat_df = harmonized_loader.load_national_month()

            deleted, inserted = replace_table(
                connection,
                "harmonized_cell_month",
                cell_df,
                HARMONIZED_CELL_MONTH_COLUMNS,
            )
            total += inserted
            print(
                "  "
                f"{harmonized_loader.cell_month_path.name}: {inserted:,} rows"
                f" (replaced {deleted:,} old rows)"
            )

            deleted, inserted = replace_table(
                connection,
                "harmonized_national_month",
                nat_df,
                HARMONIZED_NATIONAL_MONTH_COLUMNS,
            )
            total += inserted
            print(
                "  "
                f"{harmonized_loader.national_month_path.name}: {inserted:,} rows"
                f" (replaced {deleted:,} old rows)"
            )

            analyzed_tables.extend(["harmonized_cell_month", "harmonized_national_month"])

        with connection.transaction():
            for table_name in analyzed_tables:
                connection.execute(f"ANALYZE {table_name}")
    finally:
        raw.close()

    elapsed = time.perf_counter() - started
    print(f"Done: {total:,} rows loaded in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()
