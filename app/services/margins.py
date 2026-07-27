from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.dates import parse_end_date, parse_start_date
from app.db.models import InvoiceLine, QboExpense
from app.services.shepherd_report_store import get_shepherd_report, get_shepherd_report_categories


@dataclass
class CategoryMargin:
    category: str
    revenue: float
    cogs: float
    gross_margin: float
    gross_margin_pct: float
    allocated_overhead: float
    net_margin: float
    net_margin_pct: float


@dataclass
class ClientMargin:
    client_name: str
    revenue: float
    cogs: float
    gross_margin: float
    gross_margin_pct: float
    allocated_overhead: float
    net_margin: float
    net_margin_pct: float


@dataclass
class PracticeSummary:
    period_start: datetime
    period_end: datetime
    revenue: float
    cogs: float
    gross_margin: float
    gross_margin_pct: float
    service_sales: float
    inventory_sales: float
    overhead: float
    net_margin: float
    net_margin_pct: float
    categories: list[CategoryMargin]
    clients: list[ClientMargin]
    overhead_accounts: list[dict[str, float | str]]
    cogs_is_partial: bool
    using_shepherd_report: bool = False


def _qbo_expense_filters(start_date: datetime, end_date: datetime):
    end_day = parse_end_date(end_date.replace(hour=0, minute=0, second=0, microsecond=0))
    end_day_start = end_day.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        QboExpense.period_start == parse_start_date(start_date),
        QboExpense.period_end >= end_day_start,
        QboExpense.period_end <= end_day,
    )


def _deduped_qbo_expenses(db: Session, start_date: datetime, end_date: datetime) -> list[QboExpense]:
    expenses = (
        db.query(QboExpense)
        .filter(*_qbo_expense_filters(start_date, end_date))
        .order_by(QboExpense.synced_at.desc(), QboExpense.id.desc())
        .all()
    )
    by_account: dict[str, QboExpense] = {}
    for expense in expenses:
        if expense.account_name not in by_account:
            by_account[expense.account_name] = expense
    return list(by_account.values())


def _margin_from_totals(
    label: str,
    revenue: float,
    cogs: float,
    revenue_total: float,
    overhead_total: float,
) -> tuple[str, CategoryMargin]:
    gross = revenue - cogs
    share = (revenue / revenue_total) if revenue_total else 0.0
    allocated_overhead = overhead_total * share
    net = gross - allocated_overhead
    margin = CategoryMargin(
        category=label,
        revenue=revenue,
        cogs=cogs,
        gross_margin=gross,
        gross_margin_pct=(gross / revenue * 100) if revenue else 0.0,
        allocated_overhead=allocated_overhead,
        net_margin=net,
        net_margin_pct=(net / revenue * 100) if revenue else 0.0,
    )
    return label, margin


def build_practice_summary(db: Session, start_date: datetime, end_date: datetime) -> PracticeSummary:
    start_date = parse_start_date(start_date)
    end_date = parse_end_date(end_date)
    overhead_expenses = _deduped_qbo_expenses(db, start_date, end_date)
    overhead_total = sum(expense.amount for expense in overhead_expenses)

    shepherd_report = get_shepherd_report(db, start_date, end_date)
    using_shepherd_report = shepherd_report is not None and (
        shepherd_report.revenue > 0 or shepherd_report.cogs > 0
    )

    if using_shepherd_report and shepherd_report is not None:
        revenue_total = float(shepherd_report.revenue)
        cogs_total = float(shepherd_report.cogs)
        service_sales = float(shepherd_report.service_sales)
        inventory_sales = float(shepherd_report.inventory_sales)
        cogs_is_partial = False

        report_categories = get_shepherd_report_categories(db, start_date, end_date)
        category_rows = [
            (row.category, row.revenue, row.cogs)
            for row in report_categories
            if row.revenue or row.cogs
        ]
    else:
        revenue_total = (
            db.query(func.coalesce(func.sum(InvoiceLine.revenue), 0.0))
            .filter(InvoiceLine.line_date >= start_date, InvoiceLine.line_date <= end_date)
            .scalar()
            or 0.0
        )
        cogs_total = (
            db.query(func.coalesce(func.sum(InvoiceLine.cogs), 0.0))
            .filter(InvoiceLine.line_date >= start_date, InvoiceLine.line_date <= end_date)
            .scalar()
            or 0.0
        )
        inventory_sales = (
            db.query(func.coalesce(func.sum(InvoiceLine.revenue), 0.0))
            .filter(
                InvoiceLine.line_date >= start_date,
                InvoiceLine.line_date <= end_date,
                InvoiceLine.cogs > 0,
            )
            .scalar()
            or 0.0
        )
        service_sales = revenue_total - inventory_sales
        cogs_is_partial = revenue_total > 0 and (cogs_total / revenue_total) < 0.15

        category_rows = (
            db.query(
                InvoiceLine.category,
                func.sum(InvoiceLine.revenue).label("revenue"),
                func.sum(InvoiceLine.cogs).label("cogs"),
            )
            .filter(InvoiceLine.line_date >= start_date, InvoiceLine.line_date <= end_date)
            .group_by(InvoiceLine.category)
            .order_by(func.sum(InvoiceLine.revenue).desc())
            .all()
        )

    gross_margin = revenue_total - cogs_total
    net_margin = gross_margin - overhead_total

    categories: list[CategoryMargin] = []
    for row in category_rows:
        if using_shepherd_report:
            category_name = str(row[0])
            category_revenue = float(row[1] or 0.0)
            category_cogs = float(row[2] or 0.0)
        else:
            category_name = row.category
            category_revenue = float(row.revenue or 0.0)
            category_cogs = float(row.cogs or 0.0)

        _, margin = _margin_from_totals(
            category_name,
            category_revenue,
            category_cogs,
            revenue_total,
            overhead_total,
        )
        categories.append(margin)

    categories.sort(key=lambda item: item.revenue, reverse=True)

    client_rows = (
        db.query(
            InvoiceLine.client_name,
            func.sum(InvoiceLine.revenue).label("revenue"),
            func.sum(InvoiceLine.cogs).label("cogs"),
        )
        .filter(InvoiceLine.line_date >= start_date, InvoiceLine.line_date <= end_date)
        .group_by(InvoiceLine.client_name)
        .order_by(func.sum(InvoiceLine.revenue).desc())
        .all()
    )

    clients: list[ClientMargin] = []
    for row in client_rows:
        client_revenue = float(row.revenue or 0.0)
        client_cogs = float(row.cogs or 0.0)
        if not client_revenue and not client_cogs:
            continue

        _, margin = _margin_from_totals(
            str(row.client_name or "Unknown client"),
            client_revenue,
            client_cogs,
            revenue_total,
            overhead_total,
        )
        clients.append(
            ClientMargin(
                client_name=margin.category,
                revenue=margin.revenue,
                cogs=margin.cogs,
                gross_margin=margin.gross_margin,
                gross_margin_pct=margin.gross_margin_pct,
                allocated_overhead=margin.allocated_overhead,
                net_margin=margin.net_margin,
                net_margin_pct=margin.net_margin_pct,
            )
        )

    clients.sort(key=lambda item: item.revenue, reverse=True)

    overhead_accounts = [
        {
            "account_name": expense.account_name,
            "account_group": expense.account_group,
            "amount": expense.amount,
        }
        for expense in sorted(overhead_expenses, key=lambda item: item.amount, reverse=True)
    ]

    return PracticeSummary(
        period_start=start_date,
        period_end=end_date,
        revenue=revenue_total,
        cogs=cogs_total,
        gross_margin=gross_margin,
        gross_margin_pct=(gross_margin / revenue_total * 100) if revenue_total else 0.0,
        service_sales=service_sales,
        inventory_sales=inventory_sales,
        overhead=overhead_total,
        net_margin=net_margin,
        net_margin_pct=(net_margin / revenue_total * 100) if revenue_total else 0.0,
        categories=categories,
        clients=clients,
        overhead_accounts=overhead_accounts,
        cogs_is_partial=cogs_is_partial,
        using_shepherd_report=using_shepherd_report,
    )
