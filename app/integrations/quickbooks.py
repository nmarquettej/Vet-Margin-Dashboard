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
    token = db.query(QboToken).filter(QboToken.realm_id == realm_id).first()
    if token is None:
        token = QboToken(realm_id=realm_id, access_token="", refresh_token="", expires_at=expires_at)
        db.add(token)

    token.access_token = auth_client.access_token
    token.refresh_token = auth_client.refresh_token
    token.expires_at = expires_at
    db.commit()
    db.refresh(token)
    return token


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


def _parse_report_rows(rows: list[dict[str, Any]], group: str = "Expenses") -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []

    for row in rows or []:
        row_type = row.get("type")
        if row_type == "Section":
            section_group = row.get("group") or group
            parsed.extend(_parse_report_rows(row.get("Rows", {}).get("Row", []), section_group))
            continue

        if row_type != "Data":
            nested = row.get("Rows", {}).get("Row", [])
            if nested:
                parsed.extend(_parse_report_rows(nested, group))
            continue

        col_data = row.get("ColData", [])
        if len(col_data) < 2:
            continue

        account_name = (col_data[0].get("value") or "").strip()
        amount_raw = (col_data[1].get("value") or "0").replace(",", "")
        if not account_name:
            continue

        try:
            amount = float(amount_raw)
        except ValueError:
            amount = 0.0

        if amount == 0:
            continue

        parsed.append(
            {
                "account_name": account_name,
                "account_group": group,
                "amount": amount,
            }
        )

    return parsed


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

    rows = report.get("Rows", {}).get("Row", [])
    expenses = _parse_report_rows(rows)

    excluded_groups = {"Income", "COGS", "GrossProfit", "NetIncome", "NetOperatingIncome", "OtherIncome"}
    filtered: list[dict[str, Any]] = []
    for expense in expenses:
        group = expense["account_group"]
        name_lower = expense["account_name"].lower()
        if group in excluded_groups:
            continue
        if "cost of goods" in name_lower or name_lower in {"cogs", "cost of goods sold"}:
            continue
        filtered.append(expense)

    return filtered


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
