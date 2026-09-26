"""
Phase 3: Pandas/NumPy Risk Scoring Engine for The Vendor Vault.

Pulls the supplier-level aggregates built by the Phase 2 SQL view
(`v_supplier_performance_summary`) plus order-level delivery variance from
`purchase_orders`, then layers a weighted Vendor Risk Score and letter grade
on top using Pandas/NumPy. Persists the enriched result to a new
`supplier_scorecards` table and prints a ranked summary with `rich`.
"""

import os
import sqlite3

import numpy as np
import pandas as pd
from rich import box
from rich.console import Console
from rich.table import Table

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vendor_vault.db")

console = Console(width=200)


def load_supplier_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM v_supplier_performance_summary;", conn)


def load_delivery_variance(conn: sqlite3.Connection) -> pd.DataFrame:
    """Order-level delivery variance (days) per supplier, for dispersion stats."""
    return pd.read_sql_query(
        """
        SELECT
            supplier_id,
            julianday(actual_delivery_date) - julianday(promised_delivery_date)
                AS delivery_variance_days
        FROM purchase_orders;
        """,
        conn,
    )


def compute_lead_time_std_dev(variance_df: pd.DataFrame) -> pd.DataFrame:
    """Sample standard deviation (ddof=1) of delivery variance, per supplier."""
    std_dev = (
        variance_df.groupby("supplier_id")["delivery_variance_days"]
        .std(ddof=1)
        .reset_index(name="lead_time_std_dev")
    )
    # A supplier with a single order has an undefined sample std dev (NaN).
    # Treat that as zero variance observed rather than dropping the vendor.
    std_dev["lead_time_std_dev"] = std_dev["lead_time_std_dev"].fillna(0.0)
    return std_dev


def build_scorecard(summary_df: pd.DataFrame, std_dev_df: pd.DataFrame) -> pd.DataFrame:
    df = summary_df.merge(std_dev_df, on="supplier_id", how="left")

    # --- Core metric formulations ---
    df["otif_rate"] = (df["otif_orders"] / df["total_orders"]) * 100
    df["defect_rate"] = (df["total_defective_units"] / df["total_received_units"]) * 100

    # --- Weighted composite risk score (0-100, higher is better) ---
    otif_component = df["otif_rate"] * 0.50
    quality_component = np.clip(100 - (df["defect_rate"] * 10), 0, 100) * 0.30
    consistency_component = np.clip(100 - (df["lead_time_std_dev"] * 20), 0, 100) * 0.20

    df["composite_score"] = (otif_component + quality_component + consistency_component).round(1)

    # --- Letter grade assignment ---
    grade_bins = [-np.inf, 40, 55, 70, 85, np.inf]
    grade_labels = ["F", "D", "C", "B", "A"]
    df["grade"] = pd.cut(df["composite_score"], bins=grade_bins, labels=grade_labels, right=False)

    return df.sort_values("composite_score", ascending=False).reset_index(drop=True)


def save_scorecards(conn: sqlite3.Connection, df: pd.DataFrame) -> None:
    out_df = df.copy()
    out_df["grade"] = out_df["grade"].astype(str)
    out_df.to_sql("supplier_scorecards", conn, if_exists="replace", index=False)
    conn.commit()
    console.print(f"[green]Wrote[/green] {len(out_df)} rows to supplier_scorecards")


def print_scorecard_table(df: pd.DataFrame) -> None:
    table = Table(title="Vendor Risk Scorecard (ranked by composite score)", box=box.ASCII)
    table.add_column("Rank", justify="right")
    table.add_column("Supplier Name", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("OTIF %", justify="right")
    table.add_column("Defect %", justify="right")
    table.add_column("Std Dev (Days)", justify="right")
    table.add_column("Composite Score", justify="right")
    table.add_column("Grade", justify="center")

    for rank, row in enumerate(df.itertuples(index=False), start=1):
        table.add_row(
            str(rank),
            row.supplier_name,
            row.category,
            f"{row.otif_rate:.1f}",
            f"{row.defect_rate:.2f}",
            f"{row.lead_time_std_dev:.2f}",
            f"{row.composite_score:.1f}",
            row.grade,
        )

    console.print(table)

    grade_counts = df["grade"].value_counts().reindex(["A", "B", "C", "D", "F"], fill_value=0)
    console.print(f"Grade distribution: {dict(grade_counts)}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        summary_df = load_supplier_summary(conn)
        variance_df = load_delivery_variance(conn)
        std_dev_df = compute_lead_time_std_dev(variance_df)

        scorecard_df = build_scorecard(summary_df, std_dev_df)

        save_scorecards(conn, scorecard_df)
        print_scorecard_table(scorecard_df)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
