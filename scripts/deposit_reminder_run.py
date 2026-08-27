#!/usr/bin/env python3
"""Whisker Waggin' appointment deposit reminder — LIVE mode runner.

Qualifies clients from Shepherd, optionally sends Gmail reminders + staff report
via Composio. Prefer correct non-sends over false positives.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("America/New_York")
GMAIL_USER = "noreply@whiskerwagginwnc.com"
GMAIL_VERSION = "20260721_00"
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAILURE_TO = "nmarquettej@gmail.com"
PET_PORTAL = "https://petportal.vet/whisker-waggin-wnc/payment/0"

CANCEL_STATUSES = {
    "canceled",
    "cancelled",
    "checked out",
    "no show",
    "noshow",
}

# Previously tracked unpaid from automation memory (for paid-since).
# Updated by the agent after each run via memory; seed here for this run.
PRIOR_UNPAID = [
    {
        "client_id": "faaebc9b-77f1-4dd9-8d50-b44e00db5dde",
        "client_name": "Heather Bybee",
        "appointment_ids": [
            "345a3404-6cbe-470b-bfad-b4a601681146",
            "66a63a00-cead-42dd-ac2b-b4a900db134d",
        ],
        "appointment_label": "Fri 9/4 2:00pm",
        "milestone": "2-week",
        "invoice_number": "299",
        "outstanding": 245.0,
        "email": "hjoub@aol.com",
    }
]

# appointment_id -> set of milestones already LIVE-sent
ALREADY_SENT: dict[str, set[str]] = {
    "1555f827-2570-419e-a21d-b48e01519e2f": {"1-week"},
    "eed3a028-dd71-4817-a713-b49100dae383": {"1-week"},
    "72bec82f-7cd7-4731-8695-b491018afaa0": {"2-week"},
    "1d7a4d29-bcde-451c-acb3-b49501776b95": {"1-week"},
    "2792a999-5908-4220-bce8-b48301070f12": {"1-week"},
    "9377b397-34cd-4739-af0b-b495017a430e": {"1-week", "48-hour"},
    "8174970a-2a7b-4525-837d-b4a200e5ceb6": {"1-week"},
    "3eefb3be-5c2c-4284-9f61-b48d00dba095": {"2-week"},
    "c5157f6f-d9b9-434a-af39-b4a500dede04": {"2-week"},
    "345a3404-6cbe-470b-bfad-b4a601681146": {"2-week"},
    "66a63a00-cead-42dd-ac2b-b4a900db134d": {"2-week"},
}


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if not val:
        raise SystemExit(f"Missing required env: {name}")
    return val


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": env("SHEPHERD_PUBLIC_KEY"),
        "X-Integration-Private-Key": env("SHEPHERD_PRIVATE_KEY"),
        "X-Clinic-Id": env("SHEPHERD_CLINIC_ID"),
    }


def shepherd_post(endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = env("SHEPHERD_BASE_URL").rstrip("/")
    with httpx.Client(timeout=90.0) as client:
        resp = client.post(
            f"{base}/{endpoint.lstrip('/')}",
            headers=shepherd_headers(),
            json=body or {},
        )
        resp.raise_for_status()
        return resp.json()


def shepherd_paginated(endpoint: str, body: dict[str, Any] | None = None, rpp: int = 500) -> list[dict]:
    page = 1
    items: list[dict] = []
    base_body = dict(body or {})
    while True:
        data = shepherd_post(endpoint, {**base_body, "page": page, "rpp": min(rpp, 1000)})
        batch = data.get("item") or data.get("items") or []
        if not isinstance(batch, list):
            break
        items.extend(batch)
        total = data.get("totalRecords") or data.get("total_records") or len(items)
        if len(items) >= total or not batch:
            break
        page += 1
    return items


def client_name(client: dict | None) -> str:
    if not isinstance(client, dict):
        return "Unknown"
    first = str(client.get("firstName") or "").strip()
    last = str(client.get("lastName") or "").strip()
    name = " ".join(p for p in (first, last) if p).strip()
    if name:
        return name
    return str(client.get("name") or client.get("displayName") or "Unknown").strip() or "Unknown"


def first_name(client: dict | None) -> str:
    if not isinstance(client, dict):
        return "there"
    fn = str(client.get("firstName") or "").strip()
    if fn:
        return fn
    full = client_name(client)
    return full.split()[0] if full and full != "Unknown" else "there"


def usable_email(client: dict | None) -> str | None:
    if not isinstance(client, dict):
        return None
    email = str(client.get("email") or "").strip()
    if not email or "@" not in email:
        return None
    # Reject obviously bad
    if email.lower() in {"n/a", "none", "null", "na"}:
        return None
    return email


def format_appt_when(start: datetime) -> str:
    local = start.astimezone(TZ)
    day = local.strftime("%A")
    md = f"{local.month}/{local.day}"
    hour = local.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day}, {md} at {hour}"


def format_currency(amount: float) -> str:
    return f"{amount:,.2f}"


def current_milestone(start: datetime, now: datetime) -> str | None:
    """Exclusive current-window tiers. Prefer correct non-sends."""
    delta = start - now
    if delta.total_seconds() <= 0:
        return None
    hours = delta.total_seconds() / 3600.0
    days = hours / 24.0
    if hours <= 48:
        return "48-hour"
    if days <= 7:
        return "1-week"
    if days <= 14:
        return "2-week"
    return None


def is_deposit_style(invoice: dict, items: list[dict] | None) -> bool:
    """Prefer non-send for large multi-service AR. Deposit-style ≈ Wellness + Travel, travel-only, small."""
    balance = float(invoice.get("balance") or 0)
    if balance <= 0:
        return False
    # Hard cap: large AR unlikely to be appointment deposit
    if balance > 400:
        return False

    names: list[str] = []
    nested = invoice.get("invoiceItems") or []
    if isinstance(nested, list):
        for it in nested:
            if isinstance(it, dict) and not it.get("isDeclined") and not it.get("deleted"):
                names.append(str(it.get("name") or "").lower())
    if items:
        for it in items:
            if str(it.get("invoiceId")) == str(invoice.get("id")):
                if not it.get("isDeclined") and not it.get("deleted"):
                    names.append(str(it.get("name") or "").lower())

    if not names:
        # No line detail — allow only modest balances as potential deposits
        return balance <= 350

    joined = " | ".join(names)
    # Exclude obvious non-deposit services
    exclude_kw = (
        "euthanasia",
        "cremation",
        "surgery",
        "hospital",
        "emergency",
        "lab",
        "blood",
        "radiograph",
        "x-ray",
        "ultrasound",
        "dental",
        "anesthesia",
    )
    if any(k in joined for k in exclude_kw):
        return False

    has_travel = any("travel" in n for n in names)
    has_wellness = any("wellness" in n or "exam" in n for n in names)
    # Classic deposit: wellness + travel, or travel-only small
    if has_travel and has_wellness and len(set(names)) <= 4:
        return True
    if has_travel and len(names) <= 2 and balance <= 100:
        return True
    # Small open Active with few lines and modest balance
    if len(names) <= 3 and balance <= 350 and (has_travel or has_wellness):
        return True
    return False


def invoice_age_hours(invoice: dict, now: datetime) -> float | None:
    created = parse_dt(invoice.get("dateCreated")) or parse_dt(invoice.get("dateIssued"))
    if not created:
        return None
    return (now - created).total_seconds() / 3600.0


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def reminder_html(first: str, when: str, balance: float, signature_html: str) -> str:
    paras = [
        f"Hey {html_escape(first)},",
        (
            f"This is a friendly reminder of your upcoming appointment with Whisker Waggin' "
            f"Mobile Veterinary Care on {html_escape(when)}."
        ),
        (
            f"Our system indicates you have not yet paid the outstanding balance on your account "
            f"in the amount of ${format_currency(balance)} for the appointment deposit. For your "
            f"convenience and to keep your visit running smoothly, we kindly ask that all appointment "
            f"deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to "
            f"your visit, we will unfortunately need to cancel the trip."
        ),
        (
            "You can view and pay your open invoice through the Pet Portal linked below. Our team "
            "can also take payment directly over the phone at the number below. If you have any "
            "questions or need assistance, please reach out to "
            '<a href="mailto:admin@whiskerwagginwnc.com">admin@whiskerwagginwnc.com</a> '
            "and our team would be happy to help."
        ),
        f'<a href="{PET_PORTAL}">{PET_PORTAL}</a>',
        "We look forward to caring for your pet soon!",
    ]
    body = "".join(f"<p>{p}</p>" for p in paras)
    return body + "<br>" + signature_html


def staff_report_html(
    report_date: str,
    sent_rows: list[dict],
    paid_rows: list[dict],
    signature_html: str,
) -> str:
    style = (
        "border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;"
        "width:100%;max-width:900px;"
    )
    th = "border:1px solid #333;padding:6px 8px;background:#f0f0f0;text-align:left;"
    td = "border:1px solid #333;padding:6px 8px;text-align:left;"

    parts = [
        f"<h2>[WWMVC] Outstanding Appointment Deposit Report — {html_escape(report_date)}</h2>",
        "<h3>Section 1 — Emails sent this run</h3>",
    ]
    if not sent_rows:
        parts.append("<p>No qualifying clients this run.</p>")
    else:
        parts.append(f'<table style="{style}"><thead><tr>')
        for col in ("Client", "Email", "Appointment", "Milestone", "Outstanding", "Credit", "Invoice"):
            parts.append(f'<th style="{th}">{col}</th>')
        parts.append("</tr></thead><tbody>")
        for r in sent_rows:
            parts.append("<tr>")
            for key in (
                "client",
                "email",
                "appointment",
                "milestone",
                "outstanding",
                "credit",
                "invoice",
            ):
                parts.append(f'<td style="{td}">{html_escape(str(r.get(key, "")))}</td>')
            parts.append("</tr>")
        parts.append("</tbody></table>")

    parts.append("<h3>Section 2 — Paid since last report</h3>")
    if not paid_rows:
        parts.append("<p>No previously reported clients have paid since the last run.</p>")
    else:
        parts.append(f'<table style="{style}"><thead><tr>')
        for col in (
            "Client",
            "Appointment",
            "Prior milestone",
            "Prior outstanding",
            "Status now",
            "Evidence",
        ):
            parts.append(f'<th style="{th}">{col}</th>')
        parts.append("</tr></thead><tbody>")
        for r in paid_rows:
            parts.append("<tr>")
            for key in (
                "client",
                "appointment",
                "prior_milestone",
                "prior_outstanding",
                "status_now",
                "evidence",
            ):
                parts.append(f'<td style="{td}">{html_escape(str(r.get(key, "")))}</td>')
            parts.append("</tr>")
        parts.append("</tbody></table>")

    return "".join(parts) + "<br>" + signature_html


def get_composio():
    from composio import Composio

    return Composio(
        api_key=env("COMPOSIO_API_KEY"),
        toolkit_versions={"gmail": GMAIL_VERSION},
    )


def gmail_execute(c, slug: str, arguments: dict) -> dict:
    return c.tools.execute(
        slug=slug,
        user_id=GMAIL_USER,
        version=GMAIL_VERSION,
        arguments=arguments,
    )


def load_signature(c) -> str:
    result = gmail_execute(c, "GMAIL_LIST_SEND_AS", {})
    data = result.get("data") or {}
    send_as = data.get("sendAs") or data.get("send_as") or []
    if not send_as:
        raise RuntimeError(f"GMAIL_LIST_SEND_AS returned no sendAs: {result!r}")
    # Prefer primary / default
    chosen = None
    for entry in send_as:
        if entry.get("isPrimary") or entry.get("isDefault") or entry.get("isPrimary", False):
            chosen = entry
            break
    if chosen is None:
        # Prefer noreply address match
        for entry in send_as:
            if str(entry.get("sendAsEmail") or entry.get("sendAs") or "").lower() == GMAIL_USER:
                chosen = entry
                break
    if chosen is None:
        chosen = send_as[0]
    sig = chosen.get("signature") or ""
    if not sig.strip():
        raise RuntimeError("Send-as signature empty — refusing to invent footer")
    return sig


def extract_message_id(result: dict) -> str:
    data = result.get("data") or {}
    for key in ("id", "messageId", "message_id", "threadId"):
        if data.get(key):
            return str(data[key])
    # Nested response
    resp = data.get("response") or {}
    if isinstance(resp, dict) and resp.get("id"):
        return str(resp["id"])
    return json.dumps(data)[:80]


def qualify(now: datetime) -> dict[str, Any]:
    date_from = (now - timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    appointments = shepherd_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "isDeleted": False,
            "embed": "client,appointmentStatus,appointmentType",
        },
    )
    invoices = shepherd_paginated(
        "open-api-invoices",
        {
            "isDeleted": False,
            "embed": "invoiceStatus,invoiceItems,client",
        },
    )

    # Active open invoices by client
    active_open: dict[str, list[dict]] = {}
    invoice_by_id: dict[str, dict] = {}
    for inv in invoices:
        inv_id = str(inv.get("id") or "")
        if inv_id:
            invoice_by_id[inv_id] = inv
        status = ((inv.get("invoiceStatus") or {}).get("name") or "").strip()
        balance = float(inv.get("balance") or 0)
        if status.lower() != "active" or balance <= 0:
            continue
        client_id = str(
            inv.get("clientId")
            or ((inv.get("client") or {}).get("id") if isinstance(inv.get("client"), dict) else "")
            or ""
        )
        if not client_id:
            continue
        active_open.setdefault(client_id, []).append(inv)

    # Need invoice items for deposit-style check when not nested
    need_item_ids = []
    for invs in active_open.values():
        for inv in invs:
            nested = inv.get("invoiceItems") or []
            if not nested:
                need_item_ids.append(str(inv.get("id")))
    invoice_items: list[dict] = []
    if need_item_ids:
        for i in range(0, len(need_item_ids), 50):
            chunk = need_item_ids[i : i + 50]
            invoice_items.extend(
                shepherd_paginated(
                    "open-api-invoice-items",
                    {
                        "invoiceIds": ",".join(chunk),
                        "isDeleted": False,
                        "embed": "product",
                    },
                )
            )

    # Payments for credit balance proxy
    payments = shepherd_paginated(
        "open-api-payments",
        {"isDeleted": False, "embed": "paymentType"},
    )
    credit_by_client: dict[str, float] = {}
    latest_pay_date: dict[str, datetime] = {}
    for pay in payments:
        if pay.get("isVoided") or pay.get("voided"):
            continue
        cid = str(pay.get("clientId") or "")
        if not cid:
            continue
        pdate = parse_dt(pay.get("paymentDate") or pay.get("dateCreated"))
        if pdate is None:
            continue
        if cid in latest_pay_date and pdate <= latest_pay_date[cid]:
            continue
        latest_pay_date[cid] = pdate
        bal = pay.get("clientBalanceAfterPayment")
        if bal is None:
            continue
        credit_by_client[cid] = float(bal)

    upcoming: list[dict] = []
    exclusions: list[dict] = []

    for appt in appointments:
        if appt.get("isDeleted"):
            continue
        start = parse_dt(appt.get("startDate"))
        if not start or start <= now:
            continue
        status_name = ((appt.get("appointmentStatus") or {}).get("name") or "").strip()
        if status_name.lower() in CANCEL_STATUSES:
            continue
        client = appt.get("client") if isinstance(appt.get("client"), dict) else None
        client_id = str(appt.get("clientId") or (client.get("id") if client else "") or "")
        if not client_id or not client:
            exclusions.append({"reason": "no client", "appt": appt.get("id")})
            continue
        email = usable_email(client)
        if not email:
            exclusions.append(
                {
                    "reason": "no email",
                    "client": client_name(client),
                    "appt": appt.get("id"),
                }
            )
            continue

        milestone = current_milestone(start, now)
        if not milestone:
            continue  # outside 14d window — not notable

        credit = float(credit_by_client.get(client_id, 0.0))
        # Exact $0.00 credit required
        if abs(credit) > 0.001:
            exclusions.append(
                {
                    "reason": f"credit ${credit:.2f}",
                    "client": client_name(client),
                    "milestone": milestone,
                    "appt": appt.get("id"),
                }
            )
            continue

        open_invs = active_open.get(client_id, [])
        deposit_invs = [inv for inv in open_invs if is_deposit_style(inv, invoice_items)]
        if not deposit_invs:
            exclusions.append(
                {
                    "reason": "no Active open deposit",
                    "client": client_name(client),
                    "milestone": milestone,
                    "open_count": len(open_invs),
                    "appt": appt.get("id"),
                }
            )
            continue

        # Prefer invoice linked to appointment if any; else earliest open deposit
        chosen = None
        for inv in deposit_invs:
            if str(inv.get("appointmentId") or "") == str(appt.get("id")):
                chosen = inv
                break
        if chosen is None:
            deposit_invs_sorted = sorted(
                deposit_invs,
                key=lambda inv: parse_dt(inv.get("dateCreated") or inv.get("dateIssued"))
                or datetime.max.replace(tzinfo=timezone.utc),
            )
            chosen = deposit_invs_sorted[0]

        age_h = invoice_age_hours(chosen, now)
        if age_h is None or age_h <= 48:
            exclusions.append(
                {
                    "reason": f"invoice age {age_h}h <= 48h",
                    "client": client_name(client),
                    "milestone": milestone,
                    "invoice": chosen.get("invoiceNumber") or chosen.get("number"),
                    "appt": appt.get("id"),
                }
            )
            continue

        appt_id = str(appt.get("id"))
        already = ALREADY_SENT.get(appt_id, set())
        if milestone in already:
            exclusions.append(
                {
                    "reason": f"milestone {milestone} already sent",
                    "client": client_name(client),
                    "milestone": milestone,
                    "appt": appt_id,
                    "still_unpaid": True,
                    "invoice": chosen.get("invoiceNumber") or chosen.get("number"),
                    "balance": float(chosen.get("balance") or 0),
                    "credit": credit,
                }
            )
            continue

        upcoming.append(
            {
                "appointment_id": appt_id,
                "client_id": client_id,
                "client_name": client_name(client),
                "first_name": first_name(client),
                "email": email,
                "start": start.isoformat(),
                "when_label": format_appt_when(start),
                "milestone": milestone,
                "outstanding": float(chosen.get("balance") or 0),
                "credit": credit,
                "invoice_id": str(chosen.get("id")),
                "invoice_number": str(
                    chosen.get("invoiceNumber") or chosen.get("number") or chosen.get("id")
                ),
                "invoice_age_hours": round(age_h, 1),
            }
        )

    # Dedupe by invoice: one email per open deposit invoice (earliest appointment)
    by_invoice: dict[str, list[dict]] = {}
    for row in upcoming:
        by_invoice.setdefault(row["invoice_id"], []).append(row)
    qualified: list[dict] = []
    sibling_marks: list[dict] = []
    for inv_id, rows in by_invoice.items():
        rows_sorted = sorted(rows, key=lambda r: r["start"])
        primary = rows_sorted[0]
        primary["sibling_appointment_ids"] = [r["appointment_id"] for r in rows_sorted[1:]]
        qualified.append(primary)
        for sib in rows_sorted[1:]:
            sibling_marks.append(
                {
                    "appointment_id": sib["appointment_id"],
                    "milestone": primary["milestone"],
                    "invoice_id": inv_id,
                    "primary_appointment_id": primary["appointment_id"],
                    "client_name": primary["client_name"],
                }
            )

    # Paid-since: prior unpaid who no longer qualify as unpaid deposit
    paid_since: list[dict] = []
    still_unpaid_tracking: list[dict] = []

    # Index current state by client
    for prior in PRIOR_UNPAID:
        cid = prior["client_id"]
        credit = float(credit_by_client.get(cid, 0.0))
        open_invs = active_open.get(cid, [])
        deposit_invs = [inv for inv in open_invs if is_deposit_style(inv, invoice_items)]
        # Check if prior invoice still open Active with balance
        prior_inv_num = str(prior.get("invoice_number") or "")
        matching = []
        for inv in open_invs:
            num = str(inv.get("invoiceNumber") or inv.get("number") or "")
            if prior_inv_num and num == prior_inv_num:
                matching.append(inv)

        status_now = None
        evidence = None
        if abs(credit) > 0.001:
            status_now = "Paid / credit on account"
            evidence = f"credit now ${credit:.2f}"
        elif matching:
            bal = float(matching[0].get("balance") or 0)
            if bal <= 0:
                status_now = "Balance cleared"
                evidence = f"invoice #{prior_inv_num} balance ${bal:.2f}"
            else:
                # Still unpaid
                still_unpaid_tracking.append(
                    {
                        **prior,
                        "outstanding": bal,
                        "credit": credit,
                    }
                )
                continue
        elif not deposit_invs:
            # Check if invoice exists but paid/checked out
            prior_inv_obj = None
            for inv in invoices:
                num = str(inv.get("invoiceNumber") or inv.get("number") or "")
                if prior_inv_num and num == prior_inv_num:
                    prior_inv_obj = inv
                    break
            if prior_inv_obj:
                st = ((prior_inv_obj.get("invoiceStatus") or {}).get("name") or "")
                bal = float(prior_inv_obj.get("balance") or 0)
                if bal <= 0 or st.lower() in {"checked out", "paid", "closed", "void"}:
                    status_now = "Balance cleared" if bal <= 0 else "No longer Active open deposit"
                    evidence = f"#{prior_inv_num} status={st} balance=${bal:.2f}"
                else:
                    status_now = "No longer Active open deposit"
                    evidence = f"#{prior_inv_num} status={st} balance=${bal:.2f}"
            else:
                status_now = "No longer Active open deposit"
                evidence = f"prior #{prior_inv_num} not found as Active open deposit"
        else:
            # Has other deposit invoices but prior one cleared?
            still_has_prior = any(
                str(inv.get("invoiceNumber") or inv.get("number") or "") == prior_inv_num
                for inv in deposit_invs
            )
            if not still_has_prior:
                status_now = "No longer Active open deposit"
                evidence = f"#{prior_inv_num} no longer Active open; other deposits may exist"
            else:
                inv = next(
                    inv
                    for inv in deposit_invs
                    if str(inv.get("invoiceNumber") or inv.get("number") or "") == prior_inv_num
                )
                still_unpaid_tracking.append(
                    {
                        **prior,
                        "outstanding": float(inv.get("balance") or 0),
                        "credit": credit,
                    }
                )
                continue

        if status_now:
            paid_since.append(
                {
                    "client": prior["client_name"],
                    "appointment": prior.get("appointment_label", ""),
                    "prior_milestone": prior.get("milestone", ""),
                    "prior_outstanding": f"${prior.get('outstanding', 0):.2f}",
                    "status_now": status_now,
                    "evidence": evidence or "",
                }
            )

    # Also track newly qualifying as unpaid for next run (after send)
    return {
        "now": now.isoformat(),
        "qualified": qualified,
        "sibling_marks": sibling_marks,
        "exclusions": exclusions,
        "paid_since": paid_since,
        "still_unpaid_tracking": still_unpaid_tracking,
        "appointment_count": len(appointments),
        "invoice_count": len(invoices),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--send", action="store_true", help="LIVE send (default if not qualify-only)")
    parser.add_argument("--out", default="/tmp/deposit_reminder_result.json")
    args = parser.parse_args()
    live = not args.qualify_only

    now = datetime.now(timezone.utc)
    try:
        result = qualify(now)
    except Exception as exc:
        err = {"ok": False, "error": f"Shepherd failure: {exc}"}
        with open(args.out, "w") as f:
            json.dump(err, f, indent=2)
        if live:
            try:
                c = get_composio()
                sig = load_signature(c)
                body = f"<p>Deposit reminder run failed (Shepherd): {html_escape(str(exc))}</p><br>{sig}"
                gmail_execute(
                    c,
                    "GMAIL_SEND_EMAIL",
                    {
                        "recipient_email": FAILURE_TO,
                        "subject": "[WWMVC] Deposit reminder run FAILED",
                        "body": body,
                        "is_html": True,
                    },
                )
            except Exception as gexc:
                err["gmail_failure_note"] = str(gexc)
                with open(args.out, "w") as f:
                    json.dump(err, f, indent=2)
        print(json.dumps(err, indent=2))
        return 1

    if args.qualify_only:
        result["ok"] = True
        result["mode"] = "qualify-only"
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps({
            "ok": True,
            "qualified_count": len(result["qualified"]),
            "qualified": result["qualified"],
            "paid_since": result["paid_since"],
            "still_unpaid": result["still_unpaid_tracking"],
            "exclusions_sample": result["exclusions"][:30],
            "exclusion_count": len(result["exclusions"]),
        }, indent=2, default=str))
        return 0

    # LIVE send
    sent_rows: list[dict] = []
    send_details: list[dict] = []
    try:
        c = get_composio()
        signature_html = load_signature(c)
    except Exception as exc:
        err = {
            "ok": False,
            "error": f"Gmail signature failure: {exc}",
            "qualified": result["qualified"],
            "paid_since": result["paid_since"],
        }
        with open(args.out, "w") as f:
            json.dump(err, f, indent=2, default=str)
        try:
            c = get_composio()
            # Cannot load signature — still try failure note without invented footer? Template says fail Gmail step.
            # Failure note to nmarquettej — try send without signature only if signature load failed for staff path.
            gmail_execute(
                c,
                "GMAIL_SEND_EMAIL",
                {
                    "recipient_email": FAILURE_TO,
                    "subject": "[WWMVC] Deposit reminder run FAILED",
                    "body": f"Gmail signature could not be loaded: {exc}",
                    "is_html": False,
                },
            )
        except Exception:
            pass
        print(json.dumps(err, indent=2, default=str))
        return 1

    for row in result["qualified"]:
        body = reminder_html(
            row["first_name"],
            row["when_label"],
            row["outstanding"],
            signature_html,
        )
        try:
            send_result = gmail_execute(
                c,
                "GMAIL_SEND_EMAIL",
                {
                    "recipient_email": row["email"],
                    "subject": CLIENT_SUBJECT,
                    "body": body,
                    "is_html": True,
                },
            )
            mid = extract_message_id(send_result)
            send_details.append({**row, "gmail_id": mid, "status": "sent"})
            sent_rows.append(
                {
                    "client": row["client_name"],
                    "email": row["email"],
                    "appointment": row["when_label"],
                    "milestone": row["milestone"],
                    "outstanding": f"${row['outstanding']:.2f}",
                    "credit": f"${row['credit']:.2f}",
                    "invoice": f"#{row['invoice_number']}",
                }
            )
        except Exception as exc:
            send_details.append({**row, "status": "failed", "error": str(exc)})

    report_date = now.astimezone(TZ).strftime("%B %-d, %Y")
    # %-d may not work on all platforms
    report_date = now.astimezone(TZ).strftime("%B %d, %Y").replace(" 0", " ")

    report_body = staff_report_html(
        report_date,
        sent_rows,
        result["paid_since"],
        signature_html,
    )
    staff_id = None
    try:
        staff_result = gmail_execute(
            c,
            "GMAIL_SEND_EMAIL",
            {
                "recipient_email": STAFF_TO,
                "subject": f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}",
                "body": report_body,
                "is_html": True,
            },
        )
        staff_id = extract_message_id(staff_result)
    except Exception as exc:
        # Try failure note
        try:
            gmail_execute(
                c,
                "GMAIL_SEND_EMAIL",
                {
                    "recipient_email": FAILURE_TO,
                    "subject": "[WWMVC] Deposit reminder run FAILED",
                    "body": f"<p>Staff report send failed: {html_escape(str(exc))}</p><br>{signature_html}",
                    "is_html": True,
                },
            )
        except Exception:
            pass
        out = {
            "ok": False,
            "error": f"Staff report failed: {exc}",
            "send_details": send_details,
            "signature_len": len(signature_html),
        }
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(json.dumps(out, indent=2, default=str))
        return 1

    # Build next unpaid tracking: still unpaid priors + newly sent this run
    next_unpaid = list(result["still_unpaid_tracking"])
    for detail in send_details:
        if detail.get("status") != "sent":
            continue
        next_unpaid.append(
            {
                "client_id": detail["client_id"],
                "client_name": detail["client_name"],
                "appointment_ids": [detail["appointment_id"]]
                + list(detail.get("sibling_appointment_ids") or []),
                "appointment_label": detail["when_label"],
                "milestone": detail["milestone"],
                "invoice_number": detail["invoice_number"],
                "outstanding": detail["outstanding"],
                "email": detail["email"],
            }
        )

    out = {
        "ok": True,
        "mode": "LIVE",
        "now": result["now"],
        "report_date": report_date,
        "staff_message_id": staff_id,
        "signature_len": len(signature_html),
        "send_details": send_details,
        "sibling_marks": result["sibling_marks"],
        "paid_since": result["paid_since"],
        "still_unpaid_tracking": result["still_unpaid_tracking"],
        "next_unpaid_tracking": next_unpaid,
        "exclusions": result["exclusions"],
        "qualified_count": len(result["qualified"]),
        "sent_count": len(sent_rows),
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(
        json.dumps(
            {
                "ok": True,
                "sent_count": len(sent_rows),
                "send_details": send_details,
                "staff_message_id": staff_id,
                "paid_since": result["paid_since"],
                "next_unpaid_tracking": next_unpaid,
                "exclusions_notable": [
                    e
                    for e in result["exclusions"]
                    if e.get("still_unpaid") or "credit" in str(e.get("reason", "")).lower()
                ][:20],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
