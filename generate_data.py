"""
Phase 1: Mock Data Generator for The Vendor Vault.

Generates two realistic, linked CSVs with no external dependencies beyond
numpy/pandas:
  - data/suppliers.csv
  - data/purchase_orders.csv

Each supplier is assigned a hidden "reliability profile" (on-time rate,
fill-rate tendency, defect tendency) that drives how its purchase orders are
generated. This is what creates realistic edge cases -- early deliveries,
late deliveries, partial shipments, over-shipments, and varying defect
rates -- and guarantees enough spread in the data for the later risk
scorecard to actually differentiate A-grade vendors from F-grade vendors.
"""

import os
import numpy as np
import pandas as pd
from datetime import timedelta

SEED = 42
rng = np.random.default_rng(SEED)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 12 months of purchasing activity ending "today"
PERIOD_END = pd.Timestamp("2026-09-29")
PERIOD_START = PERIOD_END - pd.DateOffset(months=12)

CATEGORIES = ["Raw Materials", "Packaging", "Electronics", "Components", "Logistics Services"]

# Baseline lead time (days) and unit cost range per category
CATEGORY_PROFILE = {
    "Raw Materials":      {"lead_time": 10, "cost_range": (5, 50)},
    "Packaging":          {"lead_time": 7,  "cost_range": (1, 15)},
    "Electronics":        {"lead_time": 21, "cost_range": (20, 300)},
    "Components":         {"lead_time": 14, "cost_range": (10, 120)},
    "Logistics Services": {"lead_time": 5,  "cost_range": (50, 400)},
}

SUPPLIERS_BY_CATEGORY = {
    "Raw Materials": ["Apex Industrial Supply", "Ironclad Raw Materials", "Atlas Global Materials"],
    "Packaging": ["Silverline Packaging", "BluePeak Packaging Solutions", "Clearwater Supply Chain"],
    "Electronics": ["Vertex Electronics Group", "Circuit & Sons Electronics", "Redwood Electronics"],
    "Components": ["Meridian Components Ltd.", "Granite Bay Components", "Pinnacle Manufacturing Inputs"],
    "Logistics Services": ["Harborview Logistics", "Summit Freight Partners", "Northgate Logistics Co."],
}

N_SUPPLIERS = sum(len(v) for v in SUPPLIERS_BY_CATEGORY.values())


def build_suppliers():
    records = []
    i = 0
    for category, names in SUPPLIERS_BY_CATEGORY.items():
        for name in names:
            supplier_id = f"S{i + 1:03d}"
            contract_start = PERIOD_START - pd.Timedelta(days=int(rng.integers(30, 900)))

            # Hidden reliability profile -- not written to CSV, only used to
            # generate believable, differentiated PO behavior below.
            # Deliberately spread performance across the roster: some vendors
            # are consistently excellent, some are mediocre, a few are poor.
            # fill-rate is modeled as: most orders ship at or slightly above
            # the ordered quantity (small "overage_std" noise, always >= 1.0),
            # but with probability "shortfall_prob" the order falls short by
            # roughly "shortfall_magnitude_mean". This is asymmetric on
            # purpose -- a symmetric noise model centered on 1.0 would put
            # ~50% of even the best supplier's orders under quantity, which
            # caps everyone's OTIF near 50% regardless of tier.
            tier = rng.choice(["excellent", "good", "average", "poor"], p=[0.2, 0.3, 0.3, 0.2])
            if tier == "excellent":
                profile = dict(on_time_rate=0.95, delay_std=1.5,
                                shortfall_prob=0.05, shortfall_magnitude_mean=0.10, overage_std=0.010,
                                defect_mean=0.005)
            elif tier == "good":
                profile = dict(on_time_rate=0.85, delay_std=3.0,
                                shortfall_prob=0.15, shortfall_magnitude_mean=0.12, overage_std=0.015,
                                defect_mean=0.015)
            elif tier == "average":
                profile = dict(on_time_rate=0.70, delay_std=5.0,
                                shortfall_prob=0.35, shortfall_magnitude_mean=0.15, overage_std=0.020,
                                defect_mean=0.035)
            else:  # poor
                profile = dict(on_time_rate=0.45, delay_std=8.0,
                                shortfall_prob=0.55, shortfall_magnitude_mean=0.20, overage_std=0.030,
                                defect_mean=0.08)

            records.append({
                "supplier_id": supplier_id,
                "supplier_name": name,
                "category": category,
                "contract_start_date": contract_start.date().isoformat(),
                "_tier": tier,
                "_profile": profile,
            })
            i += 1
    return records


def generate_purchase_orders(suppliers):
    rows = []
    po_counter = 1

    for supplier in suppliers:
        category = supplier["category"]
        cat_profile = CATEGORY_PROFILE[category]
        profile = supplier["_profile"]

        n_orders = int(rng.integers(18, 36))
        order_dates = pd.to_datetime(
            sorted(rng.uniform(PERIOD_START.value, PERIOD_END.value, size=n_orders).astype("int64"))
        )

        for order_date in order_dates:
            base_lead = cat_profile["lead_time"]
            lead_jitter = int(rng.integers(-2, 3))
            promised_delivery_date = order_date + timedelta(days=max(1, base_lead + lead_jitter))

            # Delay: negative = early, 0 = on time, positive = late.
            # Draw on-time/late status from the supplier's on_time_rate, then
            # layer a magnitude on top so late deliveries aren't all trivially late.
            is_on_time_draw = rng.random() < profile["on_time_rate"]
            if is_on_time_draw:
                delay_days = int(rng.integers(-3, 1))  # early or exactly on time
            else:
                delay_days = int(abs(rng.normal(loc=profile["delay_std"] * 1.5, scale=profile["delay_std"])))
                delay_days = max(1, delay_days)  # guarantee genuinely late

            actual_delivery_date = promised_delivery_date + timedelta(days=delay_days)

            ordered_qty = int(rng.integers(100, 5000))

            # Asymmetric fill-rate: usually ships in-full (or slightly over),
            # occasionally falls short by roughly shortfall_magnitude_mean.
            if rng.random() < profile["shortfall_prob"]:
                shortfall = abs(rng.normal(loc=profile["shortfall_magnitude_mean"],
                                            scale=profile["shortfall_magnitude_mean"] * 0.4))
                fill_ratio = max(0.5, 1.0 - shortfall)  # floor: never a near-total non-delivery
            else:
                fill_ratio = 1.0 + abs(rng.normal(loc=0.0, scale=profile["overage_std"]))
            received_qty = int(round(ordered_qty * fill_ratio))
            received_qty = max(0, received_qty)

            defect_rate_draw = max(0.0, rng.normal(loc=profile["defect_mean"], scale=profile["defect_mean"] * 0.6))
            defect_qty = int(round(received_qty * defect_rate_draw))
            defect_qty = min(defect_qty, received_qty)

            unit_cost = round(rng.uniform(*cat_profile["cost_range"]), 2)

            rows.append({
                "po_number": f"PO-{po_counter:05d}",
                "supplier_id": supplier["supplier_id"],
                "order_date": order_date.date().isoformat(),
                "promised_delivery_date": promised_delivery_date.date().isoformat(),
                "actual_delivery_date": actual_delivery_date.date().isoformat(),
                "ordered_qty": ordered_qty,
                "received_qty": received_qty,
                "defect_qty": defect_qty,
                "unit_cost": unit_cost,
            })
            po_counter += 1

    return rows


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    suppliers = build_suppliers()
    suppliers_df = pd.DataFrame(suppliers).drop(columns=["_tier", "_profile"])
    suppliers_path = os.path.join(OUTPUT_DIR, "suppliers.csv")
    suppliers_df.to_csv(suppliers_path, index=False)

    po_rows = generate_purchase_orders(suppliers)
    po_df = pd.DataFrame(po_rows)
    po_path = os.path.join(OUTPUT_DIR, "purchase_orders.csv")
    po_df.to_csv(po_path, index=False)

    print(f"Generated {len(suppliers_df)} suppliers -> {suppliers_path}")
    print(f"Generated {len(po_df)} purchase orders -> {po_path}")
    print("\nSupplier tiers (for sanity-checking spread, not part of the CSV):")
    tier_counts = pd.Series([s["_tier"] for s in suppliers]).value_counts()
    print(tier_counts.to_string())


if __name__ == "__main__":
    main()
