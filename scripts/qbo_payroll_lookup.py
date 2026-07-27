"""One-off script: look up YTD payroll paid to a named employee via QBO API."""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from urllib.parse import urlencode

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import QboToken
from app.integrations.quickbooks import refresh_token_if_needed

EMPLOYEE_NAME = (sys.argv[1] if len(sys.argv) > 1 else "courtney").lower()
START_DATE = sys.argv[2] if len(sys.argv) > 2 else "2026-01-01"
END_DATE = sys.argv[3] if len(sys.argv) > 3 else datetime.utcnow().strftime("%Y-%m-%d")


def parse_money(value: str) -> float:
    cleaned = (value or "").strip().replace("$", "").replace(",", "")
    if not cleaned or cleaned == "-":
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def walk_report_rows(rows: list, matches: list[list[str]], needle: str) -> None:
    for row in rows or []:
        if row.get("type") == "Data":
            cols = [c.get("value", "") for c in row.get("ColData", [])]
            if not needle or needle in " | ".join(cols).lower():
                matches.append(cols)
        else:
            walk_report_rows(row.get("Rows", {}).get("Row", []), matches, needle)


async def qbo_get(token: QboToken, db, path: str, params: dict | None = None) -> dict | None:
    token = refresh_token_if_needed(token, db)
    query = f"?{urlencode(params)}" if params else ""
    url = f"{settings.qbo_api_base}/v3/company/{token.realm_id}/{path}{query}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json",
            },
        )
    if response.status_code >= 400:
        print(f"ERROR {path}: {response.status_code} {response.text[:400]}")
        return None
    return response.json()


async def qbo_query(token: QboToken, db, sql: str) -> list[dict]:
    data = await qbo_get(token, db, "query", {"query": sql})
    if not data:
        return []
    response = data.get("QueryResponse", {})
    for key, value in response.items():
        if key not in {"startPosition", "maxResults", "totalCount"} and isinstance(value, list):
            return value
    return []


async def main() -> None:
    engine = create_engine(settings.database_url.replace("sqlite:///./", "sqlite:///"))
    Session = sessionmaker(bind=engine)
    db = Session()
    token = db.query(QboToken).order_by(QboToken.updated_at.desc()).first()
    if token is None:
        print("QuickBooks is not connected.")
        return

    print(f"Period: {START_DATE} to {END_DATE}")
    print(f"Searching for employee name: {EMPLOYEE_NAME!r}")
    print(f"QBO environment: {settings.qbo_environment}")
    print()

    employees = await qbo_query(token, db, "select * from Employee maxresults 1000")
    courtney_records = []
    print("Employees in QuickBooks:")
    for employee in employees:
        display = employee.get("DisplayName") or ""
        given = employee.get("GivenName") or ""
        family = employee.get("FamilyName") or ""
        full = display or f"{given} {family}".strip()
        active = employee.get("Active", True)
        print(f"  id={employee.get('Id')}  name={full}  active={active}")
        if EMPLOYEE_NAME in full.lower():
            courtney_records.append(employee)

    if not courtney_records:
        print(f"\nNo employee matching {EMPLOYEE_NAME!r} found.")
        return

    courtney_ids = {str(e.get("Id")) for e in courtney_records}
    courtney_names = {
        (e.get("DisplayName") or f"{e.get('GivenName', '')} {e.get('FamilyName', '')}").strip()
        for e in courtney_records
    }
    print(f"\nMatched employee IDs: {', '.join(sorted(courtney_ids))}")

    # Transaction list report — often includes payroll check payee names.
    report = await qbo_get(
        token,
        db,
        "reports/TransactionList",
        {
            "start_date": START_DATE,
            "end_date": END_DATE,
            "columns": "tx_date,txn_type,doc_num,name,memo,account_name,debt_amt,credit_amt,subt_nat_amount",
        },
    )

    # Payroll checks: sum net pay once per check (Direct Deposit Payable side).
    payroll_net: dict[tuple[str, str, str], float] = {}
    all_employee_net: dict[str, float] = {}
    if report:
        all_rows: list[list[str]] = []
        walk_report_rows(report.get("Rows", {}).get("Row", []), all_rows, "")

        for cols in all_rows:
            tx_date, tx_type, doc_num, name, memo, account, debit, credit, amount = (
                (cols + [""] * 9)[:9]
            )
            if tx_type != "Payroll Check":
                continue
            if "direct deposit payable" not in account.lower():
                continue
            net_pay = abs(parse_money(amount))
            employee_name = name.strip()
            all_employee_net[employee_name] = all_employee_net.get(employee_name, 0.0) + net_pay
            if EMPLOYEE_NAME in employee_name.lower():
                key = (tx_date, doc_num, employee_name)
                payroll_net[key] = net_pay

    if payroll_net:
        print(f"\nNet payroll deposits to matched employee(s):")
        running = 0.0
        for (tx_date, doc_num, employee_name), net_pay in sorted(payroll_net.items()):
            running += net_pay
            print(f"  {tx_date}  check #{doc_num or '-'}  {employee_name}  ${net_pay:,.2f}")
        print(f"\nTOTAL NET PAID YTD: ${running:,.2f}  ({len(payroll_net)} payroll checks)")

    if all_employee_net:
        print("\nAll employees — net payroll YTD (from payroll checks):")
        for employee_name, net_pay in sorted(all_employee_net.items(), key=lambda item: -item[1]):
            print(f"  {employee_name}: ${net_pay:,.2f}")


    # Purchase/check entities linked to employee ref.
    purchases = await qbo_query(
        token,
        db,
        f"select * from Purchase where TxnDate >= '{START_DATE}' and TxnDate <= '{END_DATE}' maxresults 1000",
    )
    purchase_total = 0.0
    purchase_hits = []
    for purchase in purchases:
        entity_ref = purchase.get("EntityRef") or {}
        payee = entity_ref.get("name") or ""
        memo = purchase.get("PrivateNote") or purchase.get("Memo") or ""
        if EMPLOYEE_NAME not in payee.lower() and EMPLOYEE_NAME not in memo.lower():
            continue
        total = float(purchase.get("TotalAmt") or 0)
        purchase_hits.append((purchase.get("TxnDate"), payee or memo, total, purchase.get("Id")))
        purchase_total += total

    if purchase_hits:
        print(f"\nPurchase/check transactions mentioning {EMPLOYEE_NAME}: {len(purchase_hits)}")
        for tx_date, payee, total, tx_id in purchase_hits:
            print(f"  {tx_date}  {payee}  ${total:,.2f}  id={tx_id}")
        print(f"Purchase total: ${purchase_total:,.2f}")

    # Payroll journal entries often use EmployeeRef on lines.
    journals = await qbo_query(
        token,
        db,
        f"select * from JournalEntry where TxnDate >= '{START_DATE}' and TxnDate <= '{END_DATE}' maxresults 1000",
    )
    journal_total = 0.0
    journal_hits = []
    for journal in journals:
        for line in journal.get("Line", []) or []:
            detail = line.get("JournalEntryLineDetail") or {}
            entity = detail.get("Entity") or {}
            entity_name = entity.get("EntityRef", {}).get("name") or ""
            description = line.get("Description") or ""
            amount = float(line.get("Amount") or 0)
            posting = detail.get("PostingType")
            account_ref = detail.get("AccountRef", {}).get("name") or ""
            text = f"{entity_name} {description} {account_ref}".lower()
            if EMPLOYEE_NAME not in text:
                continue
            signed = amount if posting == "Debit" else -amount
            journal_hits.append(
                (
                    journal.get("TxnDate"),
                    entity_name or description,
                    account_ref,
                    signed,
                    journal.get("Id"),
                )
            )
            if "wage" in account_ref.lower() or "payroll" in account_ref.lower() or "salary" in account_ref.lower():
                journal_total += abs(amount)
            elif signed > 0:
                journal_total += signed

    if journal_hits:
        print(f"\nJournal entry lines mentioning {EMPLOYEE_NAME}: {len(journal_hits)}")
        for tx_date, name, account, amount, tx_id in journal_hits[:40]:
            print(f"  {tx_date}  {name:25}  {account:25}  ${amount:,.2f}  id={tx_id}")

    # Try payroll-specific reports if available.
    for report_name in ("PayrollDetail", "EmployeeSales"):
        data = await qbo_get(
            token,
            db,
            f"reports/{report_name}",
            {"start_date": START_DATE, "end_date": END_DATE},
        )
        if data:
            print(f"\n{report_name} report is available.")

    print("\n=== Summary ===")
    if payroll_net:
        total_net = sum(payroll_net.values())
        print(f"Net payroll paid to {EMPLOYEE_NAME.title()} YTD: ${total_net:,.2f}")
    elif purchase_total:
        print(f"Purchase/check total: ${purchase_total:,.2f}")
    elif journal_total:
        print(f"Journal entry wage/payroll lines: ${journal_total:,.2f}")
    else:
        print(
            "No per-employee payroll detail found with current Accounting API scope.\n"
            "QuickBooks Payroll paycheck detail requires additional Payroll API scopes.\n"
            "Total company Wages from P&L sync may still be available in the dashboard."
        )


if __name__ == "__main__":
    asyncio.run(main())
