# FireScope Backend

FastAPI + PostgreSQL/PostGIS backend for MODIS and VIIRS active fire
detections from NASA FIRMS.

## Prerequisites

- Python 3.12+
- PostgreSQL with the PostGIS extension

On Ubuntu/Debian (replace `16` with your PostgreSQL version):
```bash
sudo apt update
sudo apt install postgresql postgresql-16-postgis-3
```

**Alternative: Docker.** This image already includes PostGIS, user and
database, so you can skip steps 2 and 3 of the database setup below:
```bash
docker run -d --name firescope-db \
    -e POSTGRES_USER=firescope_user \
    -e POSTGRES_PASSWORD=your_postgres_password \
    -e POSTGRES_DB=firescope_db \
    -p 5432:5432 \
    postgis/postgis
```

## Python environment

Make sure conda is initialized
```bash
source ~/miniconda3/etc/profile.d/conda.sh
```

Create the environment
```bash
conda create -n firescope python=3.12 -y
```

Activate it
```bash
conda activate firescope
```

Install dependencies
```bash
pip install -r requirements.txt
```

## Configuration

Copy the example settings and set your database password:
```bash
cp .env.example .env
```
The app will not start without a `.env` file.

## Database setup

1. Create the database user (use the same password as in `.env`)
```bash
sudo -u postgres psql -c "CREATE USER firescope_user WITH PASSWORD 'your_postgres_password';"
```

2. Create the database, owned by that user
```bash
sudo -u postgres createdb -O firescope_user firescope_db
```

3. Enable PostGIS (needs the `postgres` superuser, once per database)
```bash
sudo -u postgres psql -d firescope_db -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

4. Create the schema
```bash
psql -h localhost -U firescope_user -d firescope_db \
    -f database/schema.sql
```
Re-running this is safe; it also upgrades tables made by older
versions of the schema.

## Data

Download MODIS and VIIRS CSVs from
[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/download/) and place
them like this, keeping the file names FIRMS gives them:
```
data/raw/
├── modis/
│   ├── fire_archive_M-C61_<id>.csv
│   └── fire_nrt_M-C61_<id>.csv
└── viirs/
    ├── fire_archive_SV-C2_<id>.csv
    └── fire_nrt_SV-C2_<id>.csv
```
Any number of `fire_archive_*.csv` / `fire_nrt_*.csv` files is picked up.
To keep the data somewhere else, set `RAW_DATA_DIR` in `.env`.
CSVs are git-ignored, so each team member downloads their own copy.

Check the files without touching the database:
```bash
python inspect_data.py
python -m app.data.ingest --dry-run
```

Load them into PostgreSQL:
```bash
python -m app.data.ingest
```
Re-running is safe: each file's old rows are replaced, so nothing is
duplicated. Use `--truncate` to empty the table first, or
`--data-dir PATH` to load from another folder.

Notes on the data:
- The folder decides the sensor: files in `modis/` are MODIS, files in
  `viirs/` are VIIRS. A file in the wrong folder is rejected with a message.
- MODIS brightness is stored in `brightness` / `bright_t31`; VIIRS in
  `bright_ti4` / `bright_ti5`. The other sensor's columns are NULL.
  (Some VIIRS downloads use the MODIS column names; they are mapped
  to the VIIRS columns automatically.)
- `confidence` is 0–100 for MODIS and `l` / `n` / `h` for VIIRS.
- `acq_datetime` is in UTC.

## Run

Start the backend
```bash
uvicorn app.main:app --reload
```

## API

Interactive documentation (try every endpoint in the browser):
`http://localhost:8000/docs`

All endpoints are under `/api/v1`.

| Endpoint | Returns | Use it for |
|---|---|---|
| `GET /detections` | GeoJSON points, newest first | Individual hotspots on the map |
| `GET /detections/grid` | GeoJSON square cells with counts and FRP | Zoomed-out maps, heatmaps |
| `GET /detections/timeseries` | Counts and total FRP per period and sensor | Charts |
| `GET /detections/coverage` | Totals, date range, map extent | Date picker limits, initial map view |
| `GET /health` | `{"status": "healthy"}` | Checking the server is up |

### Filters

`/detections`, `/detections/grid` and `/detections/timeseries` all accept:

| Parameter | Example | Meaning |
|---|---|---|
| `bbox` | `19.3,34.7,29.7,41.8` | Map area: min_lon,min_lat,max_lon,max_lat |
| `start_date`, `end_date` | `2024-07-01` | UTC days, both inclusive |
| `sensor` | `MODIS` | `MODIS` or `VIIRS`; repeat for both, omit for all |
| `data_product` | `nrt` | `archive` or `nrt`; repeat for both, omit for all |
| `daynight` | `D` | `D` (day) or `N` (night) |
| `min_frp` | `10` | Minimum fire radiative power (MW) |

Extra parameters:
- `/detections`: `limit` (default 5000, max 50000). The response has
  `"truncated": true` when more points matched; zoom in, narrow the dates,
  or switch to `/grid`.
- `/detections/grid`: `cell_size` in degrees (default 0.1, about 11 km).
- `/detections/timeseries`: `interval` = `day`, `week`, `month` or `year`.

Examples:
```
/api/v1/detections?bbox=23.3,37.7,24.2,38.4&start_date=2024-07-01&end_date=2024-08-31
/api/v1/detections/grid?cell_size=0.25&sensor=VIIRS&start_date=2024-01-01
/api/v1/detections/timeseries?interval=month&start_date=2024-01-01&end_date=2024-12-31
```

In point responses, `brightness` is the main brightness temperature
(MODIS band 21/22 or VIIRS I-4) and `bright_secondary` the second one
(MODIS band 31 or VIIRS I-5), so both sensors fit one colour scale.

### Calling it from the frontend

Browsers only allow calls from origins listed in `CORS_ORIGINS`. The
defaults cover `localhost` ports 3000 (React), 5173 (Vite) and 4200
(Angular). Add others in `.env`:
```
CORS_ORIGINS=["http://localhost:5173", "https://your-deployed-site.example"]
```

```js
const res = await fetch(
  "http://localhost:8000/api/v1/detections?bbox=23.3,37.7,24.2,38.4&start_date=2024-07-01"
);
const geojson = await res.json(); // pass straight to Leaflet / MapLibre
```
