from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ShepherdReportData:
    revenue: float = 0.0
    service_sales: float = 0.0
    inventory_sales: float = 0.0
    cogs: float = 0.0
    gross_margin: float = 0.0
    source: str = "api"
    categories: list[dict[str, float | str]] = field(default_factory=list)


def _parse_money(value: Any) -> float:
    if value is None:
        return 0.0
    cleaned = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not cleaned or cleaned in {"-", "—"}:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


SUMMARY_KEY_MAP = {
    "revenue": "revenue",
    "total revenue": "revenue",
    "service sales": "service_sales",
    "services sales": "service_sales",
    "inventory sales": "inventory_sales",
    "cost of goods sold": "cogs",
    "cogs": "cogs",
    "cost of goods sold cogs": "cogs",
    "margin": "gross_margin",
    "gross margin": "gross_margin",
    "total margin": "gross_margin",
}


def parse_report_api_payload(payload: dict[str, Any]) -> ShepherdReportData | None:
    items = payload.get("item") or payload.get("items") or payload
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return None

    data = ShepherdReportData(source="api")
    categories: list[dict[str, float | str]] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        name = str(
            item.get("name")
            or item.get("categoryName")
            or item.get("productCategoryName")
            or item.get("label")
            or ""
        ).strip()
        revenue = _parse_money(
            item.get("revenue")
            or item.get("totalRevenue")
            or item.get("sales")
            or item.get("amount")
        )
        cogs = _parse_money(
            item.get("costOfGoodsSold")
            or item.get("cogs")
            or item.get("cost")
            or item.get("totalCost")
        )
        service_sales = _parse_money(item.get("serviceSales") or item.get("serviceRevenue"))
        inventory_sales = _parse_money(item.get("inventorySales") or item.get("inventoryRevenue"))

        if name and (revenue or cogs):
            categories.append(
                {
                    "category": name,
                    "revenue": revenue,
                    "cogs": cogs,
                }
            )
            continue

        if revenue and not data.revenue:
            data.revenue = revenue
        if cogs and not data.cogs:
            data.cogs = cogs
        if service_sales and not data.service_sales:
            data.service_sales = service_sales
        if inventory_sales and not data.inventory_sales:
            data.inventory_sales = inventory_sales

        for key, value in item.items():
            mapped = SUMMARY_KEY_MAP.get(_normalize_key(str(key)))
            if not mapped:
                continue
            amount = _parse_money(value)
            if amount:
                setattr(data, mapped, amount)

    if categories:
        data.categories = categories
        if not data.revenue:
            data.revenue = sum(float(row["revenue"]) for row in categories)
        if not data.cogs:
            data.cogs = sum(float(row["cogs"]) for row in categories)

    if not data.revenue and not data.cogs:
        return None

    if not data.gross_margin:
        data.gross_margin = data.revenue - data.cogs
    if not data.service_sales and data.revenue and data.inventory_sales:
        data.service_sales = max(data.revenue - data.inventory_sales, 0.0)
    if not data.inventory_sales and data.revenue and data.service_sales:
        data.inventory_sales = max(data.revenue - data.service_sales, 0.0)

    return data


def _apply_summary_pair(data: ShepherdReportData, label: str, amount: float) -> None:
    mapped = SUMMARY_KEY_MAP.get(_normalize_key(label))
    if mapped:
        setattr(data, mapped, amount)


def parse_report_csv(text: str) -> ShepherdReportData:
    data = ShepherdReportData(source="csv")
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return data

    categories: list[dict[str, float | str]] = []

    for row in rows:
        if len(row) >= 2 and not _looks_like_header_row(row):
            left = row[0].strip()
            right = _parse_money(row[1])
            if right and _normalize_key(left) in SUMMARY_KEY_MAP:
                _apply_summary_pair(data, left, right)
                continue

        parsed = _parse_category_row(row)
        if parsed:
            categories.append(parsed)

    if not categories:
        dict_reader = csv.DictReader(io.StringIO(text))
        for row in dict_reader:
            if not row:
                continue
            label = _pick_dict(row, "metric", "name", "label", "category", "product category")
            value = _parse_money(
                _pick_dict(row, "value", "amount", "total", "revenue", "cogs", "cost")
            )
            if label and value and _normalize_key(label) in SUMMARY_KEY_MAP:
                _apply_summary_pair(data, label, value)
                continue

            category = _pick_dict(
                row,
                "category",
                "product category",
                "service category",
                "name",
                "account",
            )
            revenue = _parse_money(
                _pick_dict(row, "revenue", "sales", "total revenue", "amount", "total")
            )
            cogs = _parse_money(
                _pick_dict(row, "cogs", "cost", "cost of goods sold", "total cost", "product cost")
            )
            if category and (revenue or cogs):
                categories.append({"category": category, "revenue": revenue, "cogs": cogs})

    if categories:
        data.categories = categories
        if not data.revenue:
            data.revenue = sum(float(row["revenue"]) for row in categories)
        if not data.cogs:
            data.cogs = sum(float(row["cogs"]) for row in categories)

    if not data.gross_margin:
        data.gross_margin = data.revenue - data.cogs
    if not data.service_sales and data.revenue and data.inventory_sales:
        data.service_sales = max(data.revenue - data.inventory_sales, 0.0)
    if not data.inventory_sales and data.revenue and data.service_sales:
        data.inventory_sales = max(data.revenue - data.service_sales, 0.0)

    return data


def _pick_dict(row: dict[str, str | None], *names: str) -> str:
    lowered = {_normalize_key(key): (value or "") for key, value in row.items()}
    for name in names:
        value = lowered.get(_normalize_key(name), "")
        if value:
            return value.strip()
    return ""


def _looks_like_header_row(row: list[str]) -> bool:
    joined = _normalize_key(" ".join(row))
    return any(token in joined for token in ("category", "revenue", "cogs", "cost", "metric", "name"))


def _parse_category_row(row: list[str]) -> dict[str, float | str] | None:
    if len(row) < 2:
        return None

    if _looks_like_header_row(row):
        return None

    label = row[0].strip()
    if _normalize_key(label) in SUMMARY_KEY_MAP:
        return None

    amounts = [_parse_money(cell) for cell in row[1:] if _parse_money(cell)]
    if not label or not amounts:
        return None

    if len(amounts) >= 2:
        return {"category": label, "revenue": amounts[0], "cogs": amounts[1]}

    return {"category": label, "revenue": amounts[0], "cogs": 0.0}
