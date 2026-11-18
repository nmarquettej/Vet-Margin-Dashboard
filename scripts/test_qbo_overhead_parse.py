"""Unit tests for QBO P&L overhead extraction (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.integrations.quickbooks import extract_overhead_from_report


def _data(name: str, amount: str) -> dict:
    return {"type": "Data", "ColData": [{"value": name}, {"value": amount}]}


def _section(header: str, children: list, summary: str, group: str | None = None) -> dict:
    row = {
        "type": "Section",
        "Header": {"ColData": [{"value": header}, {"value": ""}]},
        "Rows": {"Row": children},
        "Summary": {"ColData": [{"value": f"Total {header}"}, {"value": summary}]},
    }
    if group is not None:
        row["group"] = group
    return row


SAMPLE_REPORT = {
    "Rows": {
        "Row": [
            _section(
                "Income",
                [_data("Services", "1000.00")],
                "1000.00",
                group="Income",
            ),
            _section(
                "Cost of Goods Sold",
                [_data("Cost of Goods Sold", "100.00")],
                "100.00",
                group="COGS",
            ),
            {"type": "Section", "group": "GrossProfit", "Summary": {"ColData": [{"value": "Gross Profit"}, {"value": "900.00"}]}},
            _section(
                "Expenses",
                [
                    _data("Advertising", "74.86"),
                    _section("Automobile", [_data("Fuel", "349.41")], "463.37"),
                    _data("Equipment Rental", "112.00"),
                    _data("Insurance", "241.23"),
                    _section(
                        "Job Expenses",
                        [
                            _section(
                                "Job Materials",
                                [
                                    _data("Decks and Patios", "234.04"),
                                    _data("Plants and Soil", "353.12"),
                                    _data("Sprinklers and Drip Systems", "215.66"),
                                ],
                                "802.82",
                            )
                        ],
                        "957.89",
                    ),
                    _section(
                        "Legal & Professional Fees",
                        [
                            _data("Accounting", "640.00"),
                            _data("Bookkeeper", "55.00"),
                            _data("Lawyer", "400.00"),
                        ],
                        "1170.00",
                    ),
                    _section(
                        "Maintenance and Repair",
                        [_data("Equipment Repairs", "755.00")],
                        "940.00",
                    ),
                    _data("Meals and Entertainment", "28.49"),
                    _data("Office Expenses", "18.08"),
                    _data("Rent or Lease", "900.00"),
                    _section(
                        "Utilities",
                        [
                            _data("Gas and Electric", "200.53"),
                            _data("Telephone", "130.86"),
                        ],
                        "331.39",
                    ),
                ],
                "5237.31",
                group="Expenses",
            ),
            _section(
                "Other Expenses",
                [_data("Miscellaneous", "2916.00")],
                "2916.00",
                group="OtherExpenses",
            ),
        ]
    }
}


def test_extract_matches_section_totals() -> None:
    expenses = extract_overhead_from_report(SAMPLE_REPORT)
    total = round(sum(item["amount"] for item in expenses), 2)
    assert total == 8153.31  # 5237.31 + 2916.00

    by_name = {item["account_name"]: item["amount"] for item in expenses}
    assert by_name["Fuel"] == 349.41
    assert by_name["Automobile"] == 113.96  # parent residual 463.37 - 349.41
    assert by_name["Job Expenses"] == 155.07  # 957.89 - 802.82
    assert by_name["Maintenance and Repair"] == 185.00  # 940 - 755
    assert "Services" not in by_name
    assert "Cost of Goods Sold" not in by_name


def test_old_leaf_only_parser_would_understate() -> None:
    expenses = extract_overhead_from_report(SAMPLE_REPORT)
    parent_residuals = {
        "Automobile",
        "Job Expenses",
        "Maintenance and Repair",
        "Legal & Professional Fees",
        "Unallocated Expenses",
    }
    leaf_like = [item for item in expenses if item["account_name"] not in parent_residuals]
    leaf_total = round(sum(item["amount"] for item in leaf_like), 2)
    full_total = round(sum(item["amount"] for item in expenses), 2)
    residual_total = round(full_total - leaf_total, 2)
    assert leaf_total < full_total
    # Parent activity missed by leaf-only parsing: 113.96 + 155.07 + 185 + 75
    assert residual_total == 529.03


if __name__ == "__main__":
    test_extract_matches_section_totals()
    test_old_leaf_only_parser_would_understate()
    print("OK: QBO overhead parser captures parent residuals and section totals")
