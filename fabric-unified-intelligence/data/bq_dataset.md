## **Furniture Inventory & Sales Data Architecture**

> A global furniture retailer uses this schema to track 50,000+ SKUs across
> multiple warehouses, identify dead stock, and support rebranding strategies
> such as relaunching the "Tuscany Collection" as "The Arden Collection."

### Pre-requisite

Check if the dataset `unified_intelligence_fabric_demo` exists in BigQuery. If not, create it.

---

### **1. Core Table Structures**

Two primary tables separate what is in the warehouse from how quickly items sell.

#### **A. Product Stock Table (`furniture_stock`)**

*Tracks real-time inventory levels, physical specs, and pricing strategy.*

| Column | Type | Description |
|--------|------|-------------|
| `sku` | `STRING` | Product SKU (required) |
| `warehouse_id` | `STRING` | Warehouse location, e.g. Georgia (required) |
| `product_name` | `STRING` | Display name |
| `category` | `STRING` | Furniture category |
| `collection` | `STRING` | Product collection, e.g. "Tuscany Collection" |
| `material` | `STRING` | Primary material, e.g. "raw oak", "linen" |
| `style_tags` | `STRING` (REPEATED) | Aesthetic tags for trend matching, e.g. "organic-modern", "warm-neutrals", "raw-wood" |
| `unit_cost` | `NUMERIC` | Cost to the retailer |
| `clearance_price` | `NUMERIC` | Current markdown/clearance price, e.g. $400 |
| `competitor_price` | `NUMERIC` | Benchmark competitor price, e.g. $1,200 |
| `suggested_price` | `NUMERIC` | Strategy-recommended reposition price, e.g. $899 |
| `quantity_on_hand` | `INT64` | Physical count in warehouse |
| `quantity_reserved` | `INT64` | Sold but not yet shipped |
| `dimensions` | `RECORD` | Nested: `length_cm`, `width_cm`, `height_cm`, `weight_kg` — used for shipping cost and volumetric warehouse-rent calculations |
| `received_date` | `DATE` | Date stock was received |
| `last_updated` | `TIMESTAMP` | Last modification timestamp |

#### **B. Sales Table (`furniture_sales`)**

*Records every transaction to provide a pulse on market demand.*

| Column | Type | Description |
|--------|------|-------------|
| `sale_id` | `STRING` | Unique transaction ID |
| `sku` | `STRING` | Product SKU |
| `warehouse_id` | `STRING` | Source warehouse |
| `sale_timestamp` | `TIMESTAMP` | When the item left inventory |
| `quantity_sold` | `INT64` | Units sold |
| `sale_price` | `NUMERIC` | Actual transaction price |

---

## **2. Identifying & Managing Dead Stock**

"Dead stock" represents capital and warehouse space not generating a return. The **`dead_stock_view`** joins current stock with the most recent sale per SKU/warehouse to classify each item.

### **Stock Health Tiers**

| Tier | Range | Recommendation |
| --- | --- | --- |
| **Healthy** | 0 – 60 Days | Maintain current replenishment levels. |
| **Slow-Moving** | 61 – 120 Days | Consider floor-space reorganization or 10% discounts. |
| **Dead Stock** | 121+ Days | Immediate clearance or liquidation to free up space. |

*Example: The Tuscany Collection (raw oak tables & linen chairs) in the Georgia warehouse is classified as Dead Stock, having not sold in 120+ days. The recommendation is to relaunch as "The Arden Collection" at $899 — undercutting competitors at $1,200 while lifting margins ~600% over the $400 clearance price.*

---

## **3. Strategic Recommendations**

* **Volumetric Analysis:** Use the nested `dimensions` record to calculate volume. Prioritize clearing Dead Stock with the highest volume — those are the most expensive warehouse "tenants."
* **Automated Flagging:** The `dead_stock_view` automatically flags any item as "Dead" if its last sale was 120+ days ago or it has never sold since being received.
* **Margin Protection:** Compare `unit_cost` against `suggested_price` and `competitor_price` to determine how deep a discount is profitable. Use `clearance_price` as the floor.