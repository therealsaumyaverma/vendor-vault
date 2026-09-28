"""
Phase 4: Interactive CLI dashboard for The Vendor Vault.

A menu-driven `rich` terminal app built on top of the `vendor_vault.db`
tables/views produced by Phases 2 and 3:
  - v_supplier_performance_summary (SQL view, Phase 2)
  - supplier_scorecards            (Pandas-scored table, Phase 3)
  - suppliers / purchase_orders    (raw tables, Phase 2)
"""

import argparse
import os
import sqlite3
import sys

import pandas as pd
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vendor_vault.db")

console = Console(width=200)

GRADE_STYLES = {
    "A": "bold green",
    "B": "bold cyan",
    "C": "bold yellow",
    "D": "bold magenta",
    "F": "bold red",
}

OTIF_TARGET = 90.0
DEFECT_THRESHOLD = 2.0

RECOMMENDATIONS = {
    "A": "Preferred Partner -- Eligible for volume consolidation & multi-year contract renewals.",
    "B": "Preferred Partner -- Eligible for volume consolidation & multi-year contract renewals.",
    "C": "Monitored -- Requires quarterly review and buffer stock.",
    "D": "High Risk -- Issue Corrective Action Plan (CAPA) or initiate vendor offboarding.",
    "F": "High Risk -- Issue Corrective Action Plan (CAPA) or initiate vendor offboarding.",
}


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def grade_text(grade: str) -> Text:
    return Text(grade, style=GRADE_STYLES.get(grade, "white"))


def make_bar(value: float, max_value: float = 100.0, width: int = 24,
             good_at: str = "high") -> Text:
    """A simple ASCII meter bar, colored green/yellow/red by how close
    `value` is to the ideal end of its range ("high" = bigger is better,
    "low" = smaller is better)."""
    pct_filled = max(0.0, min(1.0, value / max_value))
    filled = int(round(width * pct_filled))
    bar_str = "#" * filled + "-" * (width - filled)

    ratio = value / max_value if max_value else 0
    if good_at == "high":
        style = "green" if ratio >= 0.85 else "yellow" if ratio >= 0.6 else "red"
    else:
        style = "green" if ratio <= 0.2 else "yellow" if ratio <= 0.5 else "red"

    return Text(f"[{bar_str}] {value:.1f}", style=style)


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def fetch_scorecards(conn: sqlite3.Connection, category: str | None = None) -> pd.DataFrame:
    """supplier_scorecards already carries every column produced by joining
    v_supplier_performance_summary with the Phase 3 risk-scoring math (it was
    built from that view), so the ranking table reads from it directly
    rather than re-joining a view against itself."""
    query = "SELECT * FROM supplier_scorecards"
    params = ()
    if category:
        query += " WHERE category = ?"
        params = (category,)
    query += " ORDER BY composite_score DESC"
    df = pd.read_sql_query(query, conn, params=params)
    df.insert(0, "rank", range(1, len(df) + 1))
    return df


def fetch_categories(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT DISTINCT category FROM suppliers ORDER BY category;")
    return [row[0] for row in cur.fetchall()]


def fetch_supplier_detail(conn: sqlite3.Connection, supplier_id: str) -> pd.Series | None:
    df = pd.read_sql_query(
        """
        SELECT sc.*, s.contract_start_date
        FROM supplier_scorecards sc
        JOIN suppliers s ON s.supplier_id = sc.supplier_id
        WHERE sc.supplier_id = ?;
        """,
        conn,
        params=(supplier_id,),
    )
    if df.empty:
        return None
    return df.iloc[0]


def fetch_monthly_trend(conn: sqlite3.Connection, supplier_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            strftime('%Y-%m', order_date)                                          AS month,
            COUNT(*)                                                                AS orders,
            SUM(
                CASE WHEN julianday(actual_delivery_date) <= julianday(promised_delivery_date)
                          AND received_qty >= ordered_qty
                     THEN 1 ELSE 0 END
            )                                                                        AS otif_orders,
            SUM(received_qty)                                                       AS received_units,
            SUM(defect_qty)                                                         AS defect_units
        FROM purchase_orders
        WHERE supplier_id = ?
        GROUP BY month
        ORDER BY month;
        """,
        conn,
        params=(supplier_id,),
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def print_banner() -> None:
    banner = Text(justify="center")
    banner.append("=" * 70 + "\n", style="bold blue")
    banner.append("THE VENDOR VAULT\n", style="bold white")
    banner.append("Procurement Risk & OTIF Analytics\n", style="italic cyan")
    banner.append("=" * 70, style="bold blue")
    console.print(Align.center(banner))
    console.print()


def print_kpi_cards(df: pd.DataFrame, scope_label: str = "Portfolio") -> None:
    total_spend = df["total_spend"].sum()
    otif_avg = 100 * df["otif_orders"].sum() / df["total_orders"].sum()
    defect_avg = 100 * df["total_defective_units"].sum() / df["total_received_units"].sum()
    at_risk_count = df["grade"].isin(["D", "F"]).sum()

    cards = [
        Panel(f"[bold]${total_spend:,.0f}[/bold]", title=f"{scope_label} Spend Managed", border_style="blue", box=box.ASCII),
        Panel(f"[bold]{otif_avg:.1f}%[/bold]", title=f"{scope_label} OTIF Average", border_style="green" if otif_avg >= OTIF_TARGET else "yellow", box=box.ASCII),
        Panel(f"[bold]{defect_avg:.2f}%[/bold]", title=f"{scope_label} Avg Defect Rate", border_style="green" if defect_avg <= DEFECT_THRESHOLD else "yellow", box=box.ASCII),
        Panel(f"[bold]{at_risk_count}[/bold] / {len(df)}", title="Suppliers at Risk (D/F)", border_style="red" if at_risk_count else "green", box=box.ASCII),
    ]
    console.print(*cards)
    console.print()


def print_executive_table(df: pd.DataFrame, title: str) -> None:
    table = Table(title=title, box=box.ASCII)
    table.add_column("Rank", justify="right")
    table.add_column("Supplier Name", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Total Spend ($)", justify="right")
    table.add_column("OTIF %", justify="right")
    table.add_column("Defect %", justify="right")
    table.add_column("Std Dev (Days)", justify="right")
    table.add_column("Composite Score", justify="right")
    table.add_column("Grade", justify="center")

    for row in df.itertuples(index=False):
        table.add_row(
            str(row.rank),
            row.supplier_name,
            row.category,
            f"{row.total_spend:,.0f}",
            f"{row.otif_rate:.1f}",
            f"{row.defect_rate:.2f}",
            f"{row.lead_time_std_dev:.2f}",
            f"{row.composite_score:.1f}",
            grade_text(row.grade),
        )

    console.print(table)
    console.print()


def print_monthly_trend(trend_df: pd.DataFrame) -> None:
    if trend_df.empty:
        console.print("[yellow]No purchase order history found for this supplier.[/yellow]")
        return

    table = Table(title="Monthly Performance Trend", box=box.ASCII)
    table.add_column("Month", justify="left")
    table.add_column("Orders", justify="right")
    table.add_column("OTIF %", justify="left")
    table.add_column("Defect %", justify="left")

    for row in trend_df.itertuples(index=False):
        otif_pct = 100 * row.otif_orders / row.orders if row.orders else 0.0
        defect_pct = 100 * row.defect_units / row.received_units if row.received_units else 0.0
        table.add_row(
            row.month,
            str(row.orders),
            make_bar(otif_pct, max_value=100, width=20, good_at="high"),
            make_bar(defect_pct, max_value=10, width=20, good_at="low"),
        )

    console.print(table)
    console.print()


def print_supplier_panel(detail: pd.Series) -> None:
    grade = detail["grade"]

    meta_lines = (
        f"[bold]{detail['supplier_name']}[/bold]  ({detail['supplier_id']})\n"
        f"Category: {detail['category']}\n"
        f"Contract Start: {detail['contract_start_date']}\n"
        f"Total Orders: {int(detail['total_orders'])}    "
        f"Total Spend: ${detail['total_spend']:,.0f}"
    )
    console.print(Panel(meta_lines, title="Vendor Scorecard Panel", box=box.ASCII, border_style="blue"))

    badge = Text(f" {grade} ", style=f"bold reverse {GRADE_STYLES.get(grade, 'white').replace('bold ', '')}")
    console.print(Panel(
        Align.center(Text.assemble(("Composite Score: ", "bold"), (f"{detail['composite_score']:.1f} / 100   ", "bold"), badge)),
        box=box.ASCII, border_style=GRADE_STYLES.get(grade, "white").split()[-1],
    ))

    consistency_pct = max(0.0, min(100.0, 100 - detail["lead_time_std_dev"] * 20))

    meters = Table.grid(padding=(0, 2))
    meters.add_column(justify="left")
    meters.add_column(justify="left")
    meters.add_row("OTIF % vs Target (90%):", make_bar(detail["otif_rate"], max_value=100, width=30, good_at="high"))
    meters.add_row("Defect % vs Threshold (<2%):", make_bar(detail["defect_rate"], max_value=10, width=30, good_at="low"))
    meters.add_row("Lead Time Consistency:", make_bar(consistency_pct, max_value=100, width=30, good_at="high"))
    console.print(Panel(meters, title="Performance Breakdown", box=box.ASCII, border_style="cyan"))

    recommendation = RECOMMENDATIONS.get(grade, "No recommendation available.")
    console.print(Panel(recommendation, title="Actionable Procurement Recommendation",
                         box=box.ASCII, border_style=GRADE_STYLES.get(grade, "white").split()[-1]))
    console.print()


# --------------------------------------------------------------------------
# Menu actions
# --------------------------------------------------------------------------

def action_executive_summary(conn: sqlite3.Connection) -> None:
    df = fetch_scorecards(conn)
    print_kpi_cards(df, scope_label="Portfolio")
    print_executive_table(df, title="Portfolio Executive Summary (ranked by composite score)")


def action_category_filter(conn: sqlite3.Connection) -> None:
    categories = fetch_categories(conn)
    console.print("[bold]Available categories:[/bold]")
    for idx, cat in enumerate(categories, start=1):
        console.print(f"  {idx}. {cat}")

    choice = Prompt.ask("Select a category (number or name)", default="1")
    if choice.strip().isdigit() and 1 <= int(choice) <= len(categories):
        category = categories[int(choice) - 1]
    else:
        matches = [c for c in categories if c.lower() == choice.strip().lower()]
        if not matches:
            console.print(f"[red]Unknown category '{choice}'.[/red]")
            return
        category = matches[0]

    show_category_summary(conn, category)


def resolve_category(conn: sqlite3.Connection, raw: str) -> str | None:
    """Case-insensitive exact match of `raw` against known categories."""
    categories = fetch_categories(conn)
    matches = [c for c in categories if c.lower() == raw.strip().lower()]
    return matches[0] if matches else None


def show_category_summary(conn: sqlite3.Connection, category: str) -> bool:
    """Print the KPI cards + ranking table for one category. Returns False
    (having already printed an error) if the category has no suppliers."""
    df = fetch_scorecards(conn, category=category)
    if df.empty:
        console.print(f"[yellow]No suppliers found in category '{category}'.[/yellow]")
        return False

    console.print()
    print_kpi_cards(df, scope_label=category)
    print_executive_table(df, title=f"{category} - Ranking (recalculated for this category)")
    return True


def normalize_supplier_id(raw: str) -> str:
    raw = raw.strip()
    if raw.isdigit():
        return f"S{int(raw):03d}"
    return raw.upper()


def show_supplier_deep_dive(conn: sqlite3.Connection, supplier_id: str) -> bool:
    """Print the deep-dive panel + monthly trend for one supplier. Returns
    False (having already printed an error) if the supplier doesn't exist."""
    detail = fetch_supplier_detail(conn, supplier_id)
    if detail is None:
        console.print(f"[red]No supplier found with id '{supplier_id}'.[/red]")
        return False

    print_supplier_panel(detail)

    trend_df = fetch_monthly_trend(conn, supplier_id)
    print_monthly_trend(trend_df)
    return True


def action_supplier_deep_dive(conn: sqlite3.Connection) -> None:
    df = fetch_scorecards(conn)[["supplier_id", "supplier_name", "category"]]
    console.print("[bold]Suppliers:[/bold]")
    for row in df.itertuples(index=False):
        console.print(f"  {row.supplier_id}  {row.supplier_name}  ({row.category})")

    raw_input_value = Prompt.ask("Enter supplier_id (e.g. S003) or number (1-15)")
    supplier_id = normalize_supplier_id(raw_input_value)

    console.print()
    show_supplier_deep_dive(conn, supplier_id)


MENU_TEXT = """\
[bold]1.[/bold] View Portfolio Executive Summary
[bold]2.[/bold] Filter by Category
[bold]3.[/bold] Inspect Supplier Deep-Dive Scorecard
[bold]4.[/bold] Exit
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="The Vendor Vault -- Procurement Risk & OTIF Analytics dashboard. "
                     "Run with no arguments for the interactive menu.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--summary", action="store_true",
                        help="Print the portfolio executive summary and exit.")
    group.add_argument("--supplier", metavar="ID",
                        help="Print the deep-dive scorecard for one supplier (e.g. S003 or 3) and exit.")
    group.add_argument("--category", metavar="NAME",
                        help="Print the ranking + KPIs for one category (e.g. Electronics) and exit.")
    return parser.parse_args()


def run_interactive(conn: sqlite3.Connection) -> None:
    print_banner()
    while True:
        console.print(Panel(MENU_TEXT, title="Menu", box=box.ASCII, border_style="blue"))
        choice = Prompt.ask("Select an option", choices=["1", "2", "3", "4"], default="1")
        console.print()

        if choice == "1":
            action_executive_summary(conn)
        elif choice == "2":
            action_category_filter(conn)
        elif choice == "3":
            action_supplier_deep_dive(conn)
        elif choice == "4":
            console.print("[bold cyan]Goodbye.[/bold cyan]")
            break


def main() -> None:
    args = parse_args()
    conn = get_connection()
    try:
        if args.summary:
            action_executive_summary(conn)
        elif args.supplier:
            ok = show_supplier_deep_dive(conn, normalize_supplier_id(args.supplier))
            if not ok:
                sys.exit(1)
        elif args.category:
            category = resolve_category(conn, args.category)
            if category is None:
                console.print(f"[red]Unknown category '{args.category}'.[/red]")
                sys.exit(1)
            ok = show_category_summary(conn, category)
            if not ok:
                sys.exit(1)
        else:
            run_interactive(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
