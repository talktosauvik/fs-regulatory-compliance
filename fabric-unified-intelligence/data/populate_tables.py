#!/usr/bin/env python3
"""
Populate the furniture_stock and furniture_sales BigQuery tables with
~50 realistic sample records each.

The data is designed so that the dead_stock_view produces all three
health tiers (Healthy, Slow-Moving, Dead Stock) and the Tuscany
Collection demo scenario works out-of-the-box.

Prerequisites:
  - google-cloud-bigquery  (pip install google-cloud-bigquery)
  - GCP credentials configured  (gcloud auth application-default login)
  - Dataset + tables already created via setup_bigquery.sh
"""

import os
import random
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone

from google.cloud import bigquery

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
if not PROJECT_ID:
    result = subprocess.run(
        ["gcloud", "config", "get-value", "project"],
        capture_output=True, text=True,
    )
    PROJECT_ID = result.stdout.strip()

if not PROJECT_ID:
    raise SystemExit(
        "ERROR: No GCP project configured.\n"
        "  Set GCP_PROJECT_ID or run: gcloud config set project <PROJECT_ID>"
    )

DATASET = "unified_intelligence_fabric_demo"
TABLE_STOCK = f"{PROJECT_ID}.{DATASET}.furniture_stock"
TABLE_SALES = f"{PROJECT_ID}.{DATASET}.furniture_sales"

TODAY = date.today()
NOW = datetime.now(timezone.utc)

random.seed(42)  # reproducible data

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
WAREHOUSES = ["WH-GA", "WH-TX", "WH-CA", "WH-NY", "WH-IL"]

COLLECTIONS = {
    "Tuscany Collection": {
        "materials": ["raw oak", "linen"],
        "style_tags": ["organic-modern", "warm-neutrals", "raw-wood"],
    },
    "Organic Living": {
        "materials": ["bamboo", "linen", "teak"],
        "style_tags": ["organic-modern", "warm-neutrals", "boucle"],
    },
    "Nordic Frost": {
        "materials": ["birch", "wool", "steel"],
        "style_tags": ["scandinavian", "minimalist", "cool-tones"],
    },
    "Metro Edge": {
        "materials": ["steel", "leather", "marble"],
        "style_tags": ["industrial", "urban", "minimalist"],
    },
    "Coastal Breeze": {
        "materials": ["rattan", "teak", "linen"],
        "style_tags": ["coastal", "relaxed", "warm-neutrals"],
    },
}

CATEGORIES = [
    "Dining Table", "Chair", "Sofa", "Bookshelf", "Coffee Table",
    "Bed Frame", "Side Table", "Console", "Desk", "Nightstand",
]

# Dimension templates by category (length_cm, width_cm, height_cm, weight_kg)
DIMENSION_TEMPLATES = {
    "Dining Table":  (200, 100, 75, 45.0),
    "Chair":         (55,  55,  90, 8.0),
    "Sofa":          (220, 95,  85, 55.0),
    "Bookshelf":     (90,  35,  180, 35.0),
    "Coffee Table":  (120, 60,  45, 20.0),
    "Bed Frame":     (210, 160, 40, 50.0),
    "Side Table":    (50,  50,  60, 10.0),
    "Console":       (130, 40,  80, 25.0),
    "Desk":          (140, 70,  75, 30.0),
    "Nightstand":    (50,  40,  55, 12.0),
}


def _sku(n: int) -> str:
    return f"SKU-{n:04d}"


def _jitter(base: float, pct: float = 0.15) -> float:
    """Return base ± pct variation, rounded to 2 decimals."""
    return round(base * random.uniform(1 - pct, 1 + pct), 2)


def _random_date_between(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, max(delta, 0)))


# ---------------------------------------------------------------------------
# 1. Build furniture_stock rows
# ---------------------------------------------------------------------------
stock_rows: list[dict] = []
sku_counter = 1

# ── Core demo rows: Tuscany Collection in WH-GA (Dead Stock) ──────────
tuscany_items = [
    {
        "sku": _sku(sku_counter := sku_counter),
        "warehouse_id": "WH-GA",
        "product_name": "Tuscany Raw Oak Dining Table",
        "category": "Dining Table",
        "collection": "Tuscany Collection",
        "material": "raw oak",
        "style_tags": ["organic-modern", "warm-neutrals", "raw-wood"],
        "unit_cost": 320,
        "clearance_price": 400,
        "competitor_price": 1200,
        "suggested_price": 899,
        "quantity_on_hand": 38,
        "quantity_reserved": 0,
        "dimensions": {"length_cm": 200, "width_cm": 100, "height_cm": 75, "weight_kg": 48.5},
        "received_date": str(TODAY - timedelta(days=210)),
        "last_updated": NOW.isoformat(),
    },
    {
        "sku": _sku(sku_counter := sku_counter + 1),
        "warehouse_id": "WH-GA",
        "product_name": "Tuscany Linen Dining Chair",
        "category": "Chair",
        "collection": "Tuscany Collection",
        "material": "linen",
        "style_tags": ["organic-modern", "warm-neutrals", "boucle"],
        "unit_cost": 95,
        "clearance_price": 120,
        "competitor_price": 350,
        "suggested_price": 249,
        "quantity_on_hand": 124,
        "quantity_reserved": 0,
        "dimensions": {"length_cm": 55, "width_cm": 55, "height_cm": 92, "weight_kg": 7.8},
        "received_date": str(TODAY - timedelta(days=210)),
        "last_updated": NOW.isoformat(),
    },
]
stock_rows.extend(tuscany_items)
# Track Tuscany SKUs so we can exclude them from sales
tuscany_skus = {item["sku"] for item in tuscany_items}

sku_counter += 1  # next available

# ── Background stock rows ─────────────────────────────────────────────
# We'll generate items across non-Tuscany collections and all warehouses.
background_collections = [c for c in COLLECTIONS if c != "Tuscany Collection"]

for _ in range(48):
    collection = random.choice(background_collections)
    coll_info = COLLECTIONS[collection]
    category = random.choice(CATEGORIES)
    material = random.choice(coll_info["materials"])
    warehouse = random.choice(WAREHOUSES)
    dims = DIMENSION_TEMPLATES[category]

    unit_cost = round(random.uniform(50, 600), 2)
    clearance_price = round(unit_cost * random.uniform(1.2, 2.0), 2)
    competitor_price = round(unit_cost * random.uniform(3.0, 5.0), 2)
    suggested_price = round(
        random.uniform(clearance_price * 1.1, competitor_price * 0.85), 2
    )

    qty_on_hand = random.randint(5, 200)
    qty_reserved = random.randint(0, min(qty_on_hand, 30))

    received_date = _random_date_between(TODAY - timedelta(days=365), TODAY - timedelta(days=30))

    stock_rows.append({
        "sku": _sku(sku_counter),
        "warehouse_id": warehouse,
        "product_name": f"{collection} {material.title()} {category}",
        "category": category,
        "collection": collection,
        "material": material,
        "style_tags": random.sample(coll_info["style_tags"], k=min(2, len(coll_info["style_tags"]))),
        "unit_cost": unit_cost,
        "clearance_price": clearance_price,
        "competitor_price": competitor_price,
        "suggested_price": suggested_price,
        "quantity_on_hand": qty_on_hand,
        "quantity_reserved": qty_reserved,
        "dimensions": {
            "length_cm": _jitter(dims[0]),
            "width_cm": _jitter(dims[1]),
            "height_cm": _jitter(dims[2]),
            "weight_kg": _jitter(dims[3]),
        },
        "received_date": str(received_date),
        "last_updated": NOW.isoformat(),
    })
    sku_counter += 1

print(f"  furniture_stock: {len(stock_rows)} rows prepared")

# ---------------------------------------------------------------------------
# 2. Build furniture_sales rows (driven by stock for referential integrity)
# ---------------------------------------------------------------------------
sales_rows: list[dict] = []

# Separate background items (non-Tuscany) for sales generation
background_items = [r for r in stock_rows if r["sku"] not in tuscany_skus]
random.shuffle(background_items)

# Assign health tiers to background items
n = len(background_items)
healthy_items = background_items[: int(n * 0.42)]       # ~20 items
slow_items = background_items[int(n * 0.42): int(n * 0.73)]  # ~15 items
dead_items = background_items[int(n * 0.73):]            # ~13 items (+ 2 Tuscany = ~15 dead)

sale_counter = 1


def _make_sale(item: dict, days_ago_min: int, days_ago_max: int) -> dict:
    global sale_counter
    sale_date = TODAY - timedelta(days=random.randint(days_ago_min, days_ago_max))
    sale_ts = datetime.combine(
        sale_date,
        datetime.min.time().replace(
            hour=random.randint(8, 20),
            minute=random.randint(0, 59),
        ),
        tzinfo=timezone.utc,
    )
    sale = {
        "sale_id": f"SALE-{sale_counter:04d}",
        "sku": item["sku"],
        "warehouse_id": item["warehouse_id"],
        "sale_timestamp": sale_ts.isoformat(),
        "quantity_sold": random.randint(1, 5),
        "sale_price": round(item["suggested_price"] * random.uniform(0.9, 1.1), 2),
    }
    sale_counter += 1
    return sale


# Healthy: recent sales (0–60 days ago)
for item in healthy_items:
    sales_rows.append(_make_sale(item, 1, 55))

# Slow-Moving: sales 61–120 days ago
for item in slow_items:
    sales_rows.append(_make_sale(item, 65, 115))

# Dead Stock (background): sales >120 days ago or no sale at all
for item in dead_items:
    if random.random() < 0.5:
        # Old sale
        sales_rows.append(_make_sale(item, 130, 300))
    # else: no sale → Dead Stock via "never sold"

# Tuscany Collection: explicitly NO sales → Dead Stock

print(f"  furniture_sales: {len(sales_rows)} rows prepared")

# ---------------------------------------------------------------------------
# 3. Insert into BigQuery
# ---------------------------------------------------------------------------
client = bigquery.Client(project=PROJECT_ID)

print(f"\n>>> Inserting into {TABLE_STOCK} ...")
errors = client.insert_rows_json(TABLE_STOCK, stock_rows)
if errors:
    print(f"  ERROR inserting stock rows: {errors}")
else:
    print(f"  ✓ {len(stock_rows)} rows inserted into furniture_stock")

print(f"\n>>> Inserting into {TABLE_SALES} ...")
errors = client.insert_rows_json(TABLE_SALES, sales_rows)
if errors:
    print(f"  ERROR inserting sales rows: {errors}")
else:
    print(f"  ✓ {len(sales_rows)} rows inserted into furniture_sales")

print("\nDone!")
