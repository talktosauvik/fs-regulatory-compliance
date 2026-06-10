# Data — Furniture Inventory & Sales

BigQuery resources for a global furniture retailer tracking 50,000+ SKUs, identifying dead stock, and supporting rebranding strategies (e.g. Tuscany Collection → The Arden Collection).

## Files

| File | Description |
|------|-------------|
| `bq_dataset.md` | Data architecture spec — table schemas, stock health tiers, and strategic recommendations |
| `setup_bigquery.sh` | Shell script that creates the dataset, tables, and view in BigQuery |
| `populate_tables.py` | Python script that inserts ~50 sample rows into each table |
| `generate_catalog.py` | Python script that generates the global product catalog (~450 SKUs) |
| `global_product_catalog.xlsx` | Mock global product catalog (Excel) |
| `global_product_catalog.csv` | Mock global product catalog (CSV) |

## Quick Start

```bash
export GCP_PROJECT_ID="your-project-id"
./setup_bigquery.sh
```

## Populate Sample Data

Create a Python virtual environment, install dependencies, and run the data population script:

```bash
python -m venv .venv
source .venv/bin/activate
pip install google-cloud-bigquery
python populate_tables.py
```

> **Note:** Requires GCP credentials (`gcloud auth application-default login`) and the tables to already exist (created via `setup_bigquery.sh`).

## Generate Product Catalog

Generate the mock global product catalog (~450 SKUs across 8 collections) in both Excel and CSV:

```bash
pip install openpyxl --index-url https://pypi.org/simple/
python generate_catalog.py
```

The first 50 SKUs match the BigQuery tables; the rest extend the catalog to simulate the retailer's full product universe.

## Resources Created

- **Dataset:** `unified_intelligence_fabric_demo`
- **`furniture_stock`** — inventory with collection, material, style tags, dimensions, and pricing strategy fields (clearance / competitor / suggested)
- **`furniture_sales`** — transaction log with timestamps and sale prices
- **`dead_stock_view`** — auto-classifies items as *Healthy*, *Slow-Moving*, or *Dead Stock* based on days since last sale
