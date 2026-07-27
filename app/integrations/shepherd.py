from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.config import settings

REVENUE_COST_REPORT_DATASOURCE = "revenueAndCostOverviewBundleSaleAnalysisReportDataSource"
REVENUE_COST_REPORT_ENDPOINT = "open-api-reports/revenue-and-cost-overview-report"
INVOICES_ENDPOINT = "open-api-invoices"
INVOICE_ITEMS_ENDPOINT = "open-api-invoice-items"
CLIENTS_ENDPOINT = "open-api-clients"
PRODUCTS_ENDPOINT = "open-api-products"
INVENTORY_JOURNALS_ENDPOINT = "open-api-inventory/transaction-journals"
PRODUCT_CATEGORIES_ENDPOINT = "open-api-product-categories"


def format_shepherd_report_datetime(value: datetime) -> str:
    """Shepherd report API expects full ISO timestamps with milliseconds."""
    if value.tzinfo is not None:
        from datetime import timezone

        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ShepherdClient:
    """Client for Shepherd OpenAPI (all endpoints use POST + JSON)."""

    def __init__(self) -> None:
        self.base_url = settings.shepherd_base_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "X-Integration-Public-Key": settings.shepherd_public_key,
            "X-Integration-Private-Key": settings.shepherd_private_key,
            "X-Clinic-Id": settings.shepherd_clinic_id,
        }

    async def _post(self, endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = body or {}
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/{endpoint.lstrip('/')}",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def fetch_paginated(
        self,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        rpp: int = 500,
    ) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        base_body = dict(body or {})

        while True:
            request_body = {**base_body, "page": page, "rpp": min(rpp, 1000)}
            data = await self._post(endpoint, request_body)
            batch = data.get("item") or data.get("items") or []
            if not isinstance(batch, list):
                break
            items.extend(batch)
            total = data.get("totalRecords") or data.get("total_records") or len(items)
            if len(items) >= total or not batch:
                break
            page += 1

        return items

    async def get_invoices(self, date_from: datetime, date_to: datetime) -> list[dict[str, Any]]:
        endpoint = settings.shepherd_invoice_endpoint or INVOICES_ENDPOINT
        return await self.fetch_paginated(
            endpoint,
            {
                "dateIssuedFrom": date_from.isoformat(),
                "dateIssuedTo": date_to.isoformat(),
                "isDeleted": False,
                "embed": "invoiceItems",
            },
        )

    async def get_clients(self, client_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(client_id for client_id in client_ids if client_id))
        if not unique_ids:
            return {}

        clients: dict[str, dict[str, Any]] = {}
        for index in range(0, len(unique_ids), 200):
            chunk = unique_ids[index : index + 200]
            batch = await self.fetch_paginated(
                CLIENTS_ENDPOINT,
                {"ids": ",".join(chunk)},
            )
            for client in batch:
                client_id = str(client.get("id") or "")
                if client_id:
                    clients[client_id] = client

        return clients

    async def get_revenue_cost_report(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """Shepherd's native Revenue & Cost Overview report."""
        try:
            payload = await self._post(
                REVENUE_COST_REPORT_ENDPOINT,
                {
                    "fromDate": format_shepherd_report_datetime(date_from),
                    "toDate": format_shepherd_report_datetime(date_to),
                    "dataSourceName": REVENUE_COST_REPORT_DATASOURCE,
                },
            )
            return payload, 200
        except httpx.HTTPStatusError as exc:
            return None, exc.response.status_code

    async def get_invoice_items(
        self,
        *,
        invoice_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []

        return await self.fetch_paginated(
            INVOICE_ITEMS_ENDPOINT,
            {
                "invoiceIds": ",".join(invoice_ids),
                "isDeleted": False,
                "embed": "productCategory,product",
            },
        )

    async def get_product_costs(self) -> dict[str, float]:
        products = await self.fetch_paginated(
            PRODUCTS_ENDPOINT,
            {"embed": "inventoryProductSetting,GroupProduct,productCategory"},
        )
        costs: dict[str, float] = {}
        for product in products:
            product_id = str(product.get("id") or "")
            if not product_id:
                continue

            inventory_setting = product.get("inventoryProductSetting") or {}
            group_product = product.get("groupProduct") or {}
            unit_cost = 0.0
            if isinstance(inventory_setting, dict):
                unit_cost = float(inventory_setting.get("cost") or 0.0)
            if unit_cost <= 0 and isinstance(group_product, dict):
                nested_setting = group_product.get("inventoryProductSetting") or {}
                if isinstance(nested_setting, dict):
                    unit_cost = float(nested_setting.get("cost") or 0.0)

            if unit_cost > 0:
                costs[product_id] = unit_cost

        return costs

    async def get_product_categories(self) -> dict[str, str]:
        categories = await self.fetch_paginated(PRODUCT_CATEGORIES_ENDPOINT, {})
        return {
            str(category.get("id")): str(category.get("name") or "Uncategorized")
            for category in categories
            if category.get("id")
        }

    async def get_inventory_cogs_by_invoice_item(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> dict[str, float]:
        journals = await self.fetch_paginated(
            INVENTORY_JOURNALS_ENDPOINT,
            {
                "dateCreatedFrom": date_from.isoformat(),
                "dateCreatedTo": date_to.isoformat(),
            },
        )

        cogs_by_item: dict[str, float] = {}
        for journal in journals:
            invoice_item_id = journal.get("invoiceItemId")
            if not invoice_item_id:
                continue

            unit_cost = float(journal.get("cost") or 0.0)
            quantity = abs(float(journal.get("quantity") or 1.0))
            line_cogs = unit_cost * quantity
            if line_cogs <= 0:
                continue

            key = str(invoice_item_id)
            cogs_by_item[key] = cogs_by_item.get(key, 0.0) + line_cogs

        return cogs_by_item


def parse_shepherd_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _category_name(item: dict[str, Any], category_names: dict[str, str]) -> str:
    product_category = item.get("productCategory") or {}
    product = item.get("product") or {}
    category_id = str(item.get("productCategoryId") or product.get("productCategoryId") or "")
    return (
        product_category.get("name")
        or category_names.get(category_id)
        or product.get("categoryName")
        or product.get("category")
        or category_id
        or "Uncategorized"
    )


def _line_revenue(item: dict[str, Any]) -> float:
    if item.get("total") is not None:
        return float(item.get("total"))
    if item.get("subtotal") is not None:
        return float(item.get("subtotal"))
    if item.get("lineTotal") is not None:
        return float(item.get("lineTotal"))

    quantity = float(item.get("productQuantity") or item.get("quantity") or 1.0)
    price = float(item.get("price") or item.get("unitPrice") or 0.0)
    discount = float(item.get("discount") or 0.0)
    return max((quantity * price) - discount, 0.0)


def _line_cogs(
    item: dict[str, Any],
    product_costs: dict[str, float],
    journal_cogs: dict[str, float],
) -> float:
    line_id = str(item.get("id") or "")
    journal_cost = journal_cogs.get(line_id, 0.0)

    product = item.get("product") or {}
    product_id = str(product.get("id") or item.get("productId") or "")
    quantity = float(item.get("productQuantity") or item.get("quantity") or 1.0)

    inventory_setting = product.get("inventoryProductSetting") or {}
    unit_cost = 0.0
    if isinstance(inventory_setting, dict):
        unit_cost = float(inventory_setting.get("cost") or 0.0)
    if unit_cost <= 0:
        unit_cost = float(
            item.get("cost")
            or item.get("unitCost")
            or product.get("cost")
            or product.get("unitCost")
            or product_costs.get(product_id, 0.0)
        )

    catalog_cost = quantity * unit_cost
    if item.get("totalCost") is not None:
        catalog_cost = max(catalog_cost, float(item.get("totalCost")))

    return max(journal_cost, catalog_cost)


def _client_name_from_record(client: dict[str, Any] | None, client_id: str = "") -> str:
    if isinstance(client, dict):
        first = str(client.get("firstName") or "").strip()
        last = str(client.get("lastName") or "").strip()
        client_name = " ".join(part for part in (first, last) if part).strip()
        if not client_name:
            client_name = str(
                client.get("name") or client.get("displayName") or client.get("fullName") or ""
            ).strip()
        if client_name:
            return client_name

    if client_id:
        return f"Client {client_id[:8]}"
    return "Unknown client"


def _client_info_from_invoice(
    invoice: dict[str, Any],
    client_lookup: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    embedded_client = invoice.get("client")
    client_id = str(
        invoice.get("clientId")
        or (embedded_client.get("id") if isinstance(embedded_client, dict) else "")
        or ""
    )

    if isinstance(embedded_client, dict) and embedded_client:
        return client_id, _client_name_from_record(embedded_client, client_id)

    if client_lookup and client_id in client_lookup:
        return client_id, _client_name_from_record(client_lookup[client_id], client_id)

    return client_id, _client_name_from_record(None, client_id)


def extract_invoice_lines(
    invoices: list[dict[str, Any]],
    invoice_items: list[dict[str, Any]],
    product_costs: dict[str, float] | None = None,
    journal_cogs: dict[str, float] | None = None,
    category_names: dict[str, str] | None = None,
    client_lookup: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    product_costs = product_costs or {}
    journal_cogs = journal_cogs or {}
    category_names = category_names or {}
    client_lookup = client_lookup or {}
    lines: list[dict[str, Any]] = []

    invoice_dates = {
        str(invoice.get("id")): (
            parse_shepherd_datetime(invoice.get("dateIssued"))
            or parse_shepherd_datetime(invoice.get("dateCreated"))
            or datetime.utcnow()
        )
        for invoice in invoices
        if invoice.get("id")
    }
    invoice_clients = {
        str(invoice.get("id")): _client_info_from_invoice(invoice, client_lookup)
        for invoice in invoices
        if invoice.get("id")
    }

    nested_items: list[dict[str, Any]] = []
    for invoice in invoices:
        nested = invoice.get("invoiceItems") or []
        if isinstance(nested, list):
            nested_items.extend(nested)

    # Prefer the dedicated invoice-items fetch; nested embed duplicates those rows.
    all_items = invoice_items if invoice_items else nested_items
    seen_ids: set[str] = set()

    for item in all_items:
        if item.get("isDeclined") or item.get("deleted"):
            continue

        line_id = str(item.get("id") or "")
        if line_id and line_id in seen_ids:
            continue
        if line_id:
            seen_ids.add(line_id)

        invoice_id = str(item.get("invoiceId") or "")
        client_id, client_name = invoice_clients.get(invoice_id, ("", "Unknown client"))
        line_date = (
            invoice_dates.get(invoice_id)
            or parse_shepherd_datetime(item.get("dateCreated"))
            or datetime.utcnow()
        )
        product_name = item.get("name") or "Unknown item"
        revenue = _line_revenue(item)
        cogs = _line_cogs(item, product_costs, journal_cogs)

        lines.append(
            {
                "shepherd_id": line_id or f"{invoice_id}:{product_name}:{revenue}",
                "invoice_id": invoice_id,
                "client_id": client_id,
                "client_name": client_name,
                "line_date": line_date,
                "product_name": product_name,
                "category": str(_category_name(item, category_names)),
                "quantity": float(item.get("productQuantity") or item.get("quantity") or 1.0),
                "revenue": revenue,
                "cogs": cogs,
            }
        )

    return lines
