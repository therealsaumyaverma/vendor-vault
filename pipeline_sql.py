"""
Phase 2: Database & SQL Pipeline for The Vendor Vault.

Loads the Phase 1 CSVs into a local SQLite database (vendor_vault.db) with
explicit schemas and indexes, then builds a persistent analytical view,
`v_supplier_performance_summary`, that computes OTIF and quality metrics
per supplier via a CTE-based SQL query.

Uses only the standard-library `sqlite3` module for all database
operations; `pandas` is used solely to read the source CSVs and `rich` to
render the console output.
"""

import csv
import os
import sqlite3

from rich import box
from rich.console import Console
from rich.table import Table

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(BASE_DIR, "vendor_vault.db")

SUPPLIERS_CSV = os.path.join(DATA_DIR, "suppliers.csv")
PURCHASE_ORDERS_CSV = os.path.join(DATA_DIR, "purchase_orders.csv")

console = Console(width=200)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """(Re)create the raw tables with explicit types and supporting indexes."""
    cur = conn.cursor()

    # Dropping first keeps re-runs of this script idempotent during development.
    cur.executescript(
        """
        DROP TABLE IF EXISTS purchase_orders;
        DROP TABLE IF EXISTS suppliers;

        CREATE TABLE suppliers (
            supplier_id         TEXT PRIMARY KEY,
            supplier_name       TEXT NOT NULL,
            category            TEXT NOT NULL,
            contract_start_date TEXT NOT NULL  -- ISO 8601 date string (YYYY-MM-DD)
        );

        CREATE TABLE purchase_orders (
            po_number               TEXT PRIMARY KEY,
            supplier_id             TEXT NOT NULL,
            order_date              TEXT NOT NULL,  -- ISO 8601 date string
            promised_delivery_date  TEXT NOT NULL,  -- ISO 8601 date string
            actual_delivery_date    TEXT NOT NULL,  -- ISO 8601 date string
            ordered_qty             INTEGER NOT NULL,
            received_qty            INTEGER NOT NULL,
            defect_qty              INTEGER NOT NULL,
            unit_cost               REAL NOT NULL,
            FOREIGN KEY (supplier_id) REFERENCES suppliers (supplier_id)
        );

        -- Indexes to keep the analytical JOIN/GROUP BY below fast as the
        -- purchase_orders table grows, and to speed up any future
        -- date-range filtering (e.g. month-over-month trend queries).
        CREATE INDEX idx_po_supplier_id ON purchase_orders (supplier_id);
        CREATE INDEX idx_po_order_date ON purchase_orders (order_date);
        CREATE INDEX idx_po_promised_delivery_date ON purchase_orders (promised_delivery_date);
        CREATE INDEX idx_po_actual_delivery_date ON purchase_orders (actual_delivery_date);
        """
    )
    conn.commit()


def _read_csv_rows(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_csv_data(conn: sqlite3.Connection) -> None:
    """Read the Phase 1 CSVs and bulk-load them into their SQLite tables."""
    cur = conn.cursor()

    supplier_rows = _read_csv_rows(SUPPLIERS_CSV)
    cur.executemany(
        """
        INSERT INTO suppliers (supplier_id, supplier_name, category, contract_start_date)
        VALUES (:supplier_id, :supplier_name, :category, :contract_start_date)
        """,
        supplier_rows,
    )

    po_rows = _read_csv_rows(PURCHASE_ORDERS_CSV)
    # CSV values arrive as strings; cast the numeric columns to their real types
    # so downstream arithmetic (SUM, AVG, comparisons) behaves correctly.
    for row in po_rows:
        row["ordered_qty"] = int(row["ordered_qty"])
        row["received_qty"] = int(row["received_qty"])
        row["defect_qty"] = int(row["defect_qty"])
        row["unit_cost"] = float(row["unit_cost"])

    cur.executemany(
        """
        INSERT INTO purchase_orders (
            po_number, supplier_id, order_date, promised_delivery_date,
            actual_delivery_date, ordered_qty, received_qty, defect_qty, unit_cost
        )
        VALUES (
            :po_number, :supplier_id, :order_date, :promised_delivery_date,
            :actual_delivery_date, :ordered_qty, :received_qty, :defect_qty, :unit_cost
        )
        """,
        po_rows,
    )
    conn.commit()

    console.print(f"[green]Loaded[/green] {len(supplier_rows)} suppliers and {len(po_rows)} purchase orders into {DB_PATH}")


# The core analytical layer: a CTE-based view computing OTIF, quality, and
# spend metrics per supplier. Kept as a persistent VIEW (rather than a
# materialized table) so it always reflects the latest purchase_orders data
# without needing to be manually refreshed.
CREATE_VIEW_SQL = """
DROP VIEW IF EXISTS v_supplier_performance_summary;

CREATE VIEW v_supplier_performance_summary AS
WITH po_flags AS (
    -- Step 1: compute per-PO delivery variance and the raw on-time / in-full flags.
    SELECT
        po.po_number,
        po.supplier_id,
        po.ordered_qty,
        po.received_qty,
        po.defect_qty,
        po.unit_cost,
        -- Delivery variance in days: negative = early, 0 = on-time, positive = late.
        julianday(po.actual_delivery_date) - julianday(po.promised_delivery_date)
            AS delivery_variance_days,
        CASE
            WHEN julianday(po.actual_delivery_date) <= julianday(po.promised_delivery_date)
            THEN 1 ELSE 0
        END AS is_on_time,
        CASE
            WHEN po.received_qty >= po.ordered_qty
            THEN 1 ELSE 0
        END AS is_in_full
    FROM purchase_orders po
),
po_otif AS (
    -- Step 2: an order is OTIF only if it was both on-time AND in-full.
    SELECT
        po_flags.*,
        CASE
            WHEN is_on_time = 1 AND is_in_full = 1
            THEN 1 ELSE 0
        END AS is_otif
    FROM po_flags
)
-- Step 3: join back to suppliers and aggregate to one row per vendor.
SELECT
    s.supplier_id,
    s.supplier_name,
    s.category,
    COUNT(po_otif.po_number)                                   AS total_orders,
    SUM(po_otif.received_qty * po_otif.unit_cost)              AS total_spend,
    SUM(po_otif.is_otif)                                       AS otif_orders,
    SUM(CASE WHEN po_otif.is_on_time = 0 THEN 1 ELSE 0 END)    AS late_orders,
    SUM(po_otif.ordered_qty)                                   AS total_ordered_units,
    SUM(po_otif.received_qty)                                  AS total_received_units,
    SUM(po_otif.defect_qty)                                    AS total_defective_units,
    AVG(po_otif.delivery_variance_days)                        AS avg_delivery_delay_days
FROM suppliers s
JOIN po_otif ON s.supplier_id = po_otif.supplier_id
GROUP BY s.supplier_id, s.supplier_name, s.category;
"""


def create_view(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_VIEW_SQL)
    conn.commit()
    console.print("[green]Created view[/green] v_supplier_performance_summary")


def print_top_vendors_by_spend(conn: sqlite3.Connection, limit: int = 5) -> None:
    cur = conn.execute(
        """
        SELECT
            supplier_name,
            category,
            total_orders,
            ROUND(total_spend, 2)                                   AS total_spend,
            otif_orders,
            ROUND(100.0 * otif_orders / total_orders, 1)            AS otif_rate_pct,
            late_orders,
            total_defective_units,
            ROUND(100.0 * total_defective_units / total_received_units, 2) AS defect_rate_pct,
            ROUND(avg_delivery_delay_days, 2)                       AS avg_delay_days
        FROM v_supplier_performance_summary
        ORDER BY total_spend DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    columns = [desc[0] for desc in cur.description]

    table = Table(title=f"Top {limit} Suppliers by Total Spend", show_lines=False, box=box.ASCII)
    for col in columns:
        is_text_col = col in ("supplier_name", "category")
        table.add_column(col, justify="left" if is_text_col else "right", no_wrap=is_text_col)

    for row in rows:
        table.add_row(*[str(value) for value in row])

    console.print(table)


def main() -> None:
    conn = get_connection()
    try:
        create_schema(conn)
        load_csv_data(conn)
        create_view(conn)
        print_top_vendors_by_spend(conn, limit=5)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
