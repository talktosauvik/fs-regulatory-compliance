#!/bin/bash
# =============================================================================
# BigQuery Setup Script — Furniture Inventory & Sales Data Architecture
#
# This script creates the dataset, tables, and a "Dead Stock" detection view
# as described in bq_dataset.md.
#
# Prerequisites:
#   - Google Cloud SDK (gcloud / bq CLI) installed and authenticated
#   - A GCP project set via:  gcloud config set project <PROJECT_ID>
#     or pass --project_id=<PROJECT_ID> to every bq command
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — edit these to match your environment
# ---------------------------------------------------------------------------
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
DATASET="unified_intelligence_fabric_demo"
LOCATION="${BQ_LOCATION:-US}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: No GCP project configured."
  echo "  Set the GCP_PROJECT_ID env var or run: gcloud config set project <PROJECT_ID>"
  exit 1
fi

echo "============================================="
echo " BigQuery Setup"
echo " Project : ${PROJECT_ID}"
echo " Dataset : ${DATASET}"
echo " Location: ${LOCATION}"
echo "============================================="

# ---------------------------------------------------------------------------
# 1. Create the dataset (if it does not already exist)
# ---------------------------------------------------------------------------
echo ""
echo ">>> Checking / creating dataset '${DATASET}' ..."

if bq --project_id="${PROJECT_ID}" show "${DATASET}" > /dev/null 2>&1; then
  echo "    Dataset '${DATASET}' already exists — skipping."
else
  bq --project_id="${PROJECT_ID}" mk \
    --dataset \
    --location="${LOCATION}" \
    --description="Furniture Inventory & Sales demo dataset for Unified Intelligence Fabric" \
    "${DATASET}"
  echo "    Dataset '${DATASET}' created."
fi

# ---------------------------------------------------------------------------
# 2. Create the furniture_stock table
# ---------------------------------------------------------------------------
TABLE_STOCK="${DATASET}.furniture_stock"

echo ""
echo ">>> Creating table '${TABLE_STOCK}' ..."

# JSON schema is required for nested RECORD fields (bq CLI does not support
# inline RECORD() syntax).
SCHEMA_FILE=$(mktemp /tmp/furniture_stock_schema.XXXXXX.json)
cat > "${SCHEMA_FILE}" <<'SCHEMA'
[
  {"name": "sku",               "type": "STRING",    "mode": "REQUIRED"},
  {"name": "warehouse_id",      "type": "STRING",    "mode": "REQUIRED"},
  {"name": "product_name",      "type": "STRING"},
  {"name": "category",          "type": "STRING"},
  {"name": "collection",        "type": "STRING"},
  {"name": "material",          "type": "STRING"},
  {"name": "style_tags",        "type": "STRING",    "mode": "REPEATED"},
  {"name": "unit_cost",         "type": "NUMERIC"},
  {"name": "clearance_price",   "type": "NUMERIC"},
  {"name": "competitor_price",  "type": "NUMERIC"},
  {"name": "suggested_price",   "type": "NUMERIC"},
  {"name": "quantity_on_hand",  "type": "INT64"},
  {"name": "quantity_reserved", "type": "INT64"},
  {"name": "dimensions",        "type": "RECORD", "fields": [
    {"name": "length_cm", "type": "FLOAT64"},
    {"name": "width_cm",  "type": "FLOAT64"},
    {"name": "height_cm", "type": "FLOAT64"},
    {"name": "weight_kg", "type": "FLOAT64"}
  ]},
  {"name": "received_date",     "type": "DATE"},
  {"name": "last_updated",      "type": "TIMESTAMP"}
]
SCHEMA

bq --project_id="${PROJECT_ID}" mk \
  --table \
  --description="Tracks real-time inventory levels and physical specifications" \
  "${TABLE_STOCK}" \
  "${SCHEMA_FILE}" \
  2>/dev/null \
  && echo "    Table '${TABLE_STOCK}' created." \
  || echo "    Table '${TABLE_STOCK}' already exists — skipping."

rm -f "${SCHEMA_FILE}"

# ---------------------------------------------------------------------------
# 3. Create the furniture_sales table
# ---------------------------------------------------------------------------
TABLE_SALES="${DATASET}.furniture_sales"

echo ""
echo ">>> Creating table '${TABLE_SALES}' ..."

bq --project_id="${PROJECT_ID}" mk \
  --table \
  --description="Records every transaction to provide a pulse on market demand" \
  "${TABLE_SALES}" \
  'sale_id:STRING,
   sku:STRING,
   warehouse_id:STRING,
   sale_timestamp:TIMESTAMP,
   quantity_sold:INT64,
   sale_price:NUMERIC' \
  2>/dev/null \
  && echo "    Table '${TABLE_SALES}' created." \
  || echo "    Table '${TABLE_SALES}' already exists — skipping."

# ---------------------------------------------------------------------------
# 4. Create the dead_stock_view
#    Flags items as Healthy / Slow-Moving / Dead Stock based on days since
#    the last sale (or items that have never sold).
# ---------------------------------------------------------------------------
VIEW_DEAD_STOCK="${DATASET}.dead_stock_view"

echo ""
echo ">>> Creating view '${VIEW_DEAD_STOCK}' ..."

bq --project_id="${PROJECT_ID}" mk \
  --use_legacy_sql=false \
  --view "
SELECT
  s.sku,
  s.warehouse_id,
  s.product_name,
  s.category,
  s.collection,
  s.material,
  s.style_tags,
  s.unit_cost,
  s.clearance_price,
  s.competitor_price,
  s.suggested_price,
  s.quantity_on_hand,
  s.quantity_reserved,
  (s.quantity_on_hand - s.quantity_reserved)                       AS available_stock,
  s.dimensions.length_cm,
  s.dimensions.width_cm,
  s.dimensions.height_cm,
  s.dimensions.weight_kg,
  ROUND(s.dimensions.length_cm
      * s.dimensions.width_cm
      * s.dimensions.height_cm, 2)                                AS volume_cm3,
  s.received_date,
  last_sale.last_sale_date,
  COALESCE(
    DATE_DIFF(CURRENT_DATE(), last_sale.last_sale_date, DAY),
    DATE_DIFF(CURRENT_DATE(), s.received_date, DAY)
  )                                                               AS days_since_last_sale,
  CASE
    WHEN last_sale.last_sale_date IS NULL
      THEN 'Dead Stock'
    WHEN DATE_DIFF(CURRENT_DATE(), last_sale.last_sale_date, DAY) <= 60
      THEN 'Healthy'
    WHEN DATE_DIFF(CURRENT_DATE(), last_sale.last_sale_date, DAY) <= 120
      THEN 'Slow-Moving'
    ELSE 'Dead Stock'
  END                                                             AS stock_health_tier,
  CASE
    WHEN last_sale.last_sale_date IS NULL
      THEN 'Never sold since received — immediate clearance recommended'
    WHEN DATE_DIFF(CURRENT_DATE(), last_sale.last_sale_date, DAY) <= 60
      THEN 'Maintain current replenishment levels'
    WHEN DATE_DIFF(CURRENT_DATE(), last_sale.last_sale_date, DAY) <= 120
      THEN 'Consider floor-space reorganization or 10% discounts'
    ELSE 'Immediate clearance or liquidation to free up space'
  END                                                             AS recommendation
FROM
  \`${PROJECT_ID}.${DATASET}.furniture_stock\` AS s
LEFT JOIN (
  SELECT
    sku,
    warehouse_id,
    DATE(MAX(sale_timestamp)) AS last_sale_date
  FROM
    \`${PROJECT_ID}.${DATASET}.furniture_sales\`
  GROUP BY
    sku, warehouse_id
) AS last_sale
  ON  s.sku          = last_sale.sku
  AND s.warehouse_id = last_sale.warehouse_id
ORDER BY
  days_since_last_sale DESC
" \
  --description="Automatically flags items by stock health tier (Healthy / Slow-Moving / Dead Stock)" \
  "${VIEW_DEAD_STOCK}" \
  2>/dev/null \
  && echo "    View '${VIEW_DEAD_STOCK}' created." \
  || echo "    View '${VIEW_DEAD_STOCK}' already exists — skipping."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
echo " Setup complete!"
echo ""
echo " Resources created in ${PROJECT_ID}:"
echo "   • ${DATASET}                  (dataset)"
echo "   • ${TABLE_STOCK}     (table)"
echo "   • ${TABLE_SALES}     (table)"
echo "   • ${VIEW_DEAD_STOCK}  (view)"
echo ""
echo " Next steps:"
echo "   bq query --use_legacy_sql=false 'SELECT * FROM \`${PROJECT_ID}.${VIEW_DEAD_STOCK}\` LIMIT 10'"
echo "============================================="
