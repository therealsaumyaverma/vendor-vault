# The Vendor Vault
### OTIF & Supplier Risk Scorecard

A self-contained procurement analytics pipeline that turns raw purchase-order
data into a weighted supplier risk scorecard — the kind of tool an SCM/MIS
analyst would build to answer the question every category manager eventually
asks: *"Which of our vendors can we actually trust, and which ones need a
corrective action plan?"*

---

## 1. Project Overview

Procurement teams live and die by three numbers: **On-Time, In-Full (OTIF)
delivery**, **quality (defect rate)**, and **cost of unreliability** (expediting
fees, safety stock, production stoppages caused by a late or short shipment).
Most organizations track these in scattered spreadsheets per buyer, which
makes it hard to answer simple questions at the portfolio level — *how many
of our vendors are actually high-risk right now, and where is our spend
concentrated relative to that risk?*

**The Vendor Vault** solves this by building an end-to-end pipeline that:

1. Ingests purchase-order-level transactional data (dates, quantities,
   defects, cost).
2. Computes OTIF compliance and quality metrics per vendor using SQL.
3. Converts those metrics into a single, weighted **0–100 Vendor Risk
   Score** and letter grade (A–F) using Pandas/NumPy.
4. Surfaces the result in an interactive terminal dashboard for procurement
   and supply-chain managers to drill into — by portfolio, by category, or
   by individual vendor.

**Business problems addressed:**

| Problem | How the Vendor Vault addresses it |
|---|---|
| *"Is this vendor reliable?"* | OTIF % combines both timeliness and completeness into one pass/fail metric per order. |
| *"Are we paying for defects?"* | Defect rate is tracked as % of received units, directly tying quality back to received volume. |
| *"Which vendors are consistent vs. erratic?"* | Lead-time standard deviation captures volatility that a simple average delay would hide. |
| *"Where should procurement focus first?"* | The composite score + letter grade rank the entire portfolio, spotlighting at-risk (D/F) vendors regardless of how much is spent with them. |
| *"What do we actually do about a bad vendor?"* | Each grade maps to a concrete recommended action (renew, monitor, or CAPA/offboard). |

---

## 2. System Architecture

```
 ┌─────────────────────┐
 │  generate_data.py    │   Synthetic data generator
 │  (Phase 1)           │   - 15 suppliers / 5 categories
 │                       │   - 400 purchase orders / 12 months
 │                       │   - tiered reliability profiles
 └──────────┬───────────┘   (excellent / good / average / poor)
            │  writes
            ▼
   data/suppliers.csv
   data/purchase_orders.csv
            │
            │  loaded by
            ▼
 ┌─────────────────────┐
 │  pipeline_sql.py     │   SQLite ingestion + analytical layer
 │  (Phase 2)           │   - typed tables + indexes
 │                       │   - CTE-based SQL: delivery variance,
 │                       │     is_on_time / is_in_full / is_otif flags
 │                       │   - persisted VIEW:
 │                       │     v_supplier_performance_summary
 └──────────┬───────────┘
            │  vendor_vault.db
            ▼
 ┌─────────────────────┐
 │  scorecard_engine.py │   Pandas / NumPy scoring engine
 │  (Phase 3)           │   - otif_rate, defect_rate,
 │                       │     lead_time_std_dev
 │                       │   - weighted composite_score (0-100)
 │                       │   - letter grade A-F
 │                       │   - writes table: supplier_scorecards
 └──────────┬───────────┘
            │  vendor_vault.db
            ▼
 ┌─────────────────────┐
 │  app.py              │   Rich-powered CLI dashboard
 │  (Phase 4)           │   - interactive menu (executive summary,
 │                       │     category filter, vendor deep-dive)
 │                       │   - non-interactive flags for scripting
 │                       │     (--summary / --category / --supplier)
 └─────────────────────┘
```

`vendor_vault.db` is the single source of truth shared by Phases 2–4:
raw tables (`suppliers`, `purchase_orders`), the analytical view
(`v_supplier_performance_summary`), and the scored table
(`supplier_scorecards`) all live in it, so any phase can be re-run
independently without recomputing the others.

---

## 3. Domain & Metric Logic

### OTIF (On-Time, In-Full)

An order is **OTIF** only if it satisfies *both* conditions — being on time
with a short shipment doesn't count, and neither does a full shipment that
arrives late:

```
is_on_time = 1  if  actual_delivery_date <= promised_delivery_date  else 0
is_in_full = 1  if  received_qty >= ordered_qty                     else 0
is_otif    = 1  if  is_on_time = 1 AND is_in_full = 1                else 0

otif_rate  = (OTIF orders / total orders) * 100
```

Delivery variance (used for both the average delay and the consistency
score) is computed as `julianday(actual_delivery_date) -
julianday(promised_delivery_date)` — negative is early, zero is exactly on
time, positive is late.

### Quality — Defect Rate

```
defect_rate = (total defective units / total received units) * 100
```

Measured against *received* units (not ordered), since that's what a
receiving dock actually inspects.

### Consistency — Lead-Time Volatility

```
lead_time_std_dev = sample standard deviation of per-order delivery variance
```

Two vendors can share the same average delay while one is highly consistent
and the other is a coin flip between early and very late — standard
deviation is what separates them. A high-variance vendor breaks
replenishment planning even if its *average* performance looks fine.

### Weighted Composite Risk Score (0–100, higher is better)

```
otif_component        = otif_rate * 0.50
quality_component      = clip(100 - defect_rate * 10, 0, 100) * 0.30
consistency_component  = clip(100 - lead_time_std_dev * 20, 0, 100) * 0.20

composite_score = round(otif_component + quality_component + consistency_component, 1)
```

| Weight | Component | Rationale |
|---|---|---|
| 50% | OTIF rate | The single biggest driver of downstream supply chain disruption. |
| 30% | Quality (defect rate) | Scaled so a 0% defect rate contributes the full 30 points, decaying to 0 at a 10%+ defect rate. |
| 20% | Delivery consistency | Scaled against a 5-day benchmark std dev — a vendor with 0 day std dev keeps the full 20 points, one at 5+ days loses them all. |

### Letter Grades & Recommended Actions

| Grade | Score Range | Recommended Procurement Action |
|---|---|---|
| **A** | ≥ 85 | Preferred Partner — eligible for volume consolidation & multi-year contract renewal. |
| **B** | 70 – 84.9 | Preferred Partner — eligible for volume consolidation & multi-year contract renewal. |
| **C** | 55 – 69.9 | Monitored — requires quarterly review and buffer stock. |
| **D** | 40 – 54.9 | High Risk — issue a Corrective Action Plan (CAPA) or begin offboarding. |
| **F** | < 40 | High Risk — issue a Corrective Action Plan (CAPA) or begin offboarding. |

---

## 4. Quickstart & Usage

```bash
# 1. Clone and enter the project
git clone <this-repo-url> vendor-vault
cd vendor-vault

# 2. Create and activate a virtual environment
python -m venv .venv
./.venv/Scripts/activate        # Windows (PowerShell/cmd)
# source .venv/bin/activate     # macOS/Linux

# 3. Install dependencies
pip install numpy pandas rich tabulate

# 4. Run the pipeline end-to-end
python generate_data.py         # Phase 1: generate synthetic CSVs
python pipeline_sql.py          # Phase 2: load SQLite + build the SQL view
python scorecard_engine.py      # Phase 3: compute risk scores & grades

# 5. Launch the dashboard
python app.py                   # interactive menu (default)
```

### Non-interactive CLI usage

For scripting, CI checks, or quick lookups without the interactive menu:

```bash
python app.py --summary                 # portfolio executive summary, then exit
python app.py --category Electronics    # category-filtered ranking + KPIs, then exit
python app.py --supplier S012           # single-vendor deep-dive scorecard, then exit
python app.py --supplier 12             # numeric IDs are also accepted
python app.py --help                    # full flag reference
```

Re-running any phase script is safe and idempotent — `pipeline_sql.py` drops
and recreates its tables/view, and `scorecard_engine.py` overwrites
`supplier_scorecards`, so the pipeline can be re-run in full whenever the
source CSVs change.

### Project Structure

```
vendor-vault/
├── generate_data.py       # Phase 1 — synthetic data generator
├── pipeline_sql.py        # Phase 2 — SQLite load + SQL analytical view
├── scorecard_engine.py    # Phase 3 — Pandas/NumPy risk scoring
├── app.py                 # Phase 4 — Rich CLI dashboard
├── data/
│   ├── suppliers.csv
│   └── purchase_orders.csv
├── vendor_vault.db         # generated by pipeline_sql.py (git-ignored)
└── README.md
```

---

## 5. Tech Stack

| Layer | Tools |
|---|---|
| Language | Python 3.12 |
| Data generation | NumPy (seeded RNG for reproducibility) |
| Database | SQLite (`sqlite3` standard library) — CTEs, views, indexes |
| Transformation & scoring | Pandas, NumPy |
| Presentation | `rich` (tables, panels, color-coded grades, ASCII meters) |

No external services, API keys, or cloud dependencies — the entire pipeline
runs offline from a single `python` invocation per phase.
