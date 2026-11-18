from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import QboExpense, QboToken

EXCLUDED_OVERHEAD_GROUPS = {
    "Income",
    "COGS",
    "GrossProfit",
    "NetIncome",
    "NetOperatingIncome",
    "OtherIncome",
    "NetOtherIncome",
}

OVERHEAD_GROUPS = {"Expenses", "OtherExpenses"}


def get_auth_client() -> AuthClient:
    return AuthClient(
        settings.qbo_client_id,
        settings.qbo_client_secret,
        settings.qbo_redirect_uri,
        settings.qbo_environment,
    )


def get_authorization_url(state: str = "vet-margin-dashboard") -> str:
    auth_client = get_auth_client()
    return auth_client.get_authorization_url([Scopes.ACCOUNTING], state_token=state)


def exchange_code_for_tokens(code: str, realm_id: str, db: Session) -> QboToken:
    auth_client = get_auth_client()
    auth_client.get_bearer_token(code, realm_id=realm_id)

    expires_at = datetime.utcnow() + timedelta(seconds=auth_client.expires_in or 3600)
    # Replace any prior connection so sandbox/production can't silently mix.
    db.query(QboToken).delete()
    token = QboToken(
        realm_id=realm_id,
        access_token=auth_client.access_token,
        refresh_token=auth_client.refresh_token,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def disconnect_qbo(db: Session) -> int:
    deleted = db.query(QboToken).delete()
    db.query(QboExpense).delete()
    db.commit()
    return deleted


def refresh_token_if_needed(token: QboToken, db: Session) -> QboToken:
    if token.expires_at > datetime.utcnow() + timedelta(minutes=5):
        return token

    auth_client = get_auth_client()
    auth_client.refresh(token.refresh_token)
    token.access_token = auth_client.access_token
    token.refresh_token = auth_client.refresh_token
    token.expires_at = datetime.utcnow() + timedelta(seconds=auth_client.expires_in or 3600)
    db.commit()
    db.refresh(token)
    return token


def _parse_money(value: Any) -> float:
    cleaned = str(value or "0").strip().replace(",", "").replace("$", "")
    if not cleaned or cleaned in {"-", "—"}:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _row_amount(col_data: list[dict[str, Any]] | None) -> float:
    if not col_data or len(col_data) < 2:
        return 0.0
    # Last money column works for Total and multi-period reports.
    return _parse_money(col_data[-1].get("value"))


def _row_label(col_data: list[dict[str, Any]] | None) -> str:
    if not col_data:
        return ""
    return (col_data[0].get("value") or "").strip()


def _collect_section_detail(
    rows: list[dict[str, Any]],
    group: str,
) -> list[dict[str, Any]]:
    """Flatten a P&L section into account lines, including parent-account residuals.

    QBO nests parent accounts as Sections. The section Summary includes activity posted
    directly to the parent. Summing only type=Data children understates overhead.
    """
    detail: list[dict[str, Any]] = []

    for row in rows or []:
        row_type = row.get("type")

        if row_type == "Section":
            children = row.get("Rows", {}).get("Row", []) or []
            child_detail = _collect_section_detail(children, group)
            child_sum = round(sum(item["amount"] for item in child_detail), 2)
            summary_amount = _row_amount((row.get("Summary") or {}).get("ColData"))
            header_label = _row_label((row.get("Header") or {}).get("ColData"))
            residual = round(summary_amount - child_sum, 2) if summary_amount else 0.0

            detail.extend(child_detail)
            if abs(residual) >= 0.005 and header_label:
                detail.append(
                    {
                        "account_name": header_label,
                        "account_group": group,
                        "amount": residual,
                    }
                )
            continue

        if row_type != "Data":
            nested = row.get("Rows", {}).get("Row", [])
            if nested:
                detail.extend(_collect_section_detail(nested, group))
            continue

        account_name = _row_label(row.get("ColData"))
        amount = _row_amount(row.get("ColData"))
        if not account_name or amount == 0:
            continue

        name_lower = account_name.lower()
        if "cost of goods" in name_lower or name_lower in {"cogs", "cost of goods sold"}:
            continue

        detail.append(
            {
                "account_name": account_name,
                "account_group": group,
                "amount": amount,
            }
        )

    return detail


def extract_overhead_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull Expenses + Other Expenses from a QBO ProfitAndLoss report payload."""
    overhead: list[dict[str, Any]] = []

    for row in report.get("Rows", {}).get("Row", []) or []:
        if row.get("type") != "Section":
            continue

        group = row.get("group") or ""
        if group not in OVERHEAD_GROUPS:
            continue

        children = row.get("Rows", {}).get("Row", []) or []
        detail = _collect_section_detail(children, group)
        detail_sum = round(sum(item["amount"] for item in detail), 2)
        summary_amount = _row_amount((row.get("Summary") or {}).get("ColData"))

        # Authoritative section total from QBO; keep a balancing line if detail is short.
        if summary_amount and abs(summary_amount - detail_sum) >= 0.005:
            detail.append(
                {
                    "account_name": f"Unallocated {group}",
                    "account_group": group,
                    "amount": round(summary_amount - detail_sum, 2),
                }
            )
        elif not detail and summary_amount:
            header = _row_label((row.get("Header") or {}).get("ColData")) or group
            detail.append(
                {
                    "account_name": header,
                    "account_group": group,
                    "amount": summary_amount,
                }
            )

        overhead.extend(detail)

    collapsed: dict[tuple[str, str], dict[str, Any]] = {}
    for expense in overhead:
        key = (expense["account_group"], expense["account_name"])
        if key in collapsed:
            collapsed[key]["amount"] = round(collapsed[key]["amount"] + expense["amount"], 2)
        else:
            collapsed[key] = dict(expense)

    return [item for item in collapsed.values() if abs(item["amount"]) >= 0.005]


async def fetch_company_name(token: QboToken, db: Session) -> str:
    token = refresh_token_if_needed(token, db)
    url = f"{settings.qbo_api_base}/v3/company/{token.realm_id}/companyinfo/{token.realm_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json",
            },
        )
        if response.status_code >= 400:
            return ""
        payload = response.json()
    info = payload.get("CompanyInfo") or {}
    return str(info.get("CompanyName") or info.get("LegalName") or "").strip()


async def fetch_profit_and_loss(
    token: QboToken,
    db: Session,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    token = refresh_token_if_needed(token, db)
    params = urlencode(
        {
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "accounting_method": "Accrual",
        }
    )
    url = f"{settings.qbo_api_base}/v3/company/{token.realm_id}/reports/ProfitAndLoss?{params}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        report = response.json()

    return extract_overhead_from_report(report)


def store_qbo_expenses(
    db: Session,
    expenses: list[dict[str, Any]],
    start_date: datetime,
    end_date: datetime,
) -> int:
    db.query(QboExpense).filter(
        QboExpense.period_start == start_date,
        QboExpense.period_end == end_date,
    ).delete()

    for expense in expenses:
        db.add(
            QboExpense(
                account_name=expense["account_name"],
                account_group=expense["account_group"],
                amount=expense["amount"],
                period_start=start_date,
                period_end=end_date,
            )
        )

    db.commit()
    return len(expenses)
