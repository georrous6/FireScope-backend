# FireScope Backend

## Setup

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

Create the database
```bash
sudo -u postgres createdb firescope_db
```

Create database schema
```bash
psql -h localhost -U firescope_user -d firescope_db \
    -f database/schema.sql
```

## Run

Start the backend
```bash
uvicorn app.main:app --reload
```

## API

To view the interactive API documentation, 
type `http://localhost:8000/docs` in your browser.