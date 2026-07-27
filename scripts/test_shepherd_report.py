"""Test Shepherd Revenue & Cost report API without printing credentials."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.dates import parse_end_date, parse_start_date
from app.integrations.shepherd import (
    REVENUE_COST_REPORT_DATASOURCE,
    REVENUE_COST_REPORT_ENDPOINT,
    ShepherdClient,
    format_shepherd_report_datetime,
)
from app.integrations.shepherd_report import parse_report_api_payload


async def main() -> None:
    start = parse_start_date(sys.argv[1] if len(sys.argv) > 1 else "2026-01-01")
    end = parse_end_date(sys.argv[2] if len(sys.argv) > 2 else "2026-07-21")

    print("Endpoint:", REVENUE_COST_REPORT_ENDPOINT)
    print("Data source:", REVENUE_COST_REPORT_DATASOURCE)
    print("fromDate:", format_shepherd_report_datetime(start))
    print("toDate:", format_shepherd_report_datetime(end))

    client = ShepherdClient()
    payload, status = await client.get_revenue_cost_report(start, end)
    print("HTTP status:", status)

    if not payload:
        print("No payload returned.")
        return

    out_path = ROOT / "scripts" / "last_report_response.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("Saved response to", out_path)

    parsed = parse_report_api_payload(payload)
    if parsed:
        print(
            "Parsed totals:",
            f"revenue={parsed.revenue:.2f}",
            f"cogs={parsed.cogs:.2f}",
            f"service={parsed.service_sales:.2f}",
            f"inventory={parsed.inventory_sales:.2f}",
            f"categories={len(parsed.categories)}",
        )
    else:
        print("Parser could not extract totals from response.")


if __name__ == "__main__":
    asyncio.run(main())
