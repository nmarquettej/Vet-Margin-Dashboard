from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.dates import parse_end_date, parse_start_date
from app.db.models import ShepherdReportCategory, ShepherdReportSnapshot
from app.integrations.shepherd_report import ShepherdReportData


def _period_filters(start_date: datetime, end_date: datetime):
    end_day = parse_end_date(end_date.replace(hour=0, minute=0, second=0, microsecond=0))
    end_day_start = end_day.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        ShepherdReportSnapshot.period_start == parse_start_date(start_date),
        ShepherdReportSnapshot.period_end >= end_day_start,
        ShepherdReportSnapshot.period_end <= end_day,
    )


def get_shepherd_report(db: Session, start_date: datetime, end_date: datetime) -> ShepherdReportSnapshot | None:
    return (
        db.query(ShepherdReportSnapshot)
        .filter(*_period_filters(start_date, end_date))
        .order_by(ShepherdReportSnapshot.synced_at.desc())
        .first()
    )


def get_shepherd_report_categories(
    db: Session,
    start_date: datetime,
    end_date: datetime,
) -> list[ShepherdReportCategory]:
    end_day = parse_end_date(end_date.replace(hour=0, minute=0, second=0, microsecond=0))
    end_day_start = end_day.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(ShepherdReportCategory)
        .filter(
            ShepherdReportCategory.period_start == parse_start_date(start_date),
            ShepherdReportCategory.period_end >= end_day_start,
            ShepherdReportCategory.period_end <= end_day,
        )
        .order_by(ShepherdReportCategory.revenue.desc())
        .all()
    )


def store_shepherd_report(
    db: Session,
    start_date: datetime,
    end_date: datetime,
    report: ShepherdReportData,
) -> ShepherdReportSnapshot:
    start_date = parse_start_date(start_date)
    end_date = parse_end_date(end_date)

    db.query(ShepherdReportSnapshot).filter(
        ShepherdReportSnapshot.period_start == start_date,
        ShepherdReportSnapshot.period_end == end_date,
    ).delete()
    db.query(ShepherdReportCategory).filter(
        ShepherdReportCategory.period_start == start_date,
        ShepherdReportCategory.period_end == end_date,
    ).delete()

    snapshot = ShepherdReportSnapshot(
        period_start=start_date,
        period_end=end_date,
        revenue=report.revenue,
        service_sales=report.service_sales,
        inventory_sales=report.inventory_sales,
        cogs=report.cogs,
        gross_margin=report.gross_margin or (report.revenue - report.cogs),
        source=report.source,
    )
    db.add(snapshot)

    for row in report.categories:
        db.add(
            ShepherdReportCategory(
                period_start=start_date,
                period_end=end_date,
                category=str(row["category"]),
                revenue=float(row["revenue"]),
                cogs=float(row["cogs"]),
            )
        )

    db.flush()
    return snapshot
