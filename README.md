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

## Run

Start the backend
```bash
uvicorn app.main:app --reload
```

## API

To view the interactive API documentation, 
type `http://localhost:8000/docs` in your browser.