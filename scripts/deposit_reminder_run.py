#!/usr/bin/env python3
"""Whisker Waggin appointment deposit reminder — LIVE / qualify-only runner.

Qualification (all required):
1. Upcoming appointment (future start; not canceled/checked out/no show)
2. Active invoice with open/outstanding balance (deposit-style preferred)
3. Client credit balance exactly $0.00
4. Usable email on file
5. Due for current-window milestone not already sent for this appointment
6. Active deposit invoice created/issued more than 48 hours ago

Milestone windows (exclusive current tier):
- <=48h → 48-hour
- <=7d (and >48h) → 1-week
- <=14d (and >7d) → 2-week
"""

from __future__ import annotations

import argparse
import html
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
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAILURE_TO = "nmarquettej@gmail.com"
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
PET_PORTAL = "https://petportal.vet/whisker-waggin-wnc/payment/0"
GMAIL_TOOLKIT_VERSION = "20260721_00"

TERMINAL_STATUSES = {
    "canceled",
    "cancelled",
    "checked out",
    "no show",
    "noshow",
}

# Previously LIVE-sent milestones (appointment_id, milestone) — keep in sync with memory.
DEFAULT_SENT_MILESTONES: set[tuple[str, str]] = {
    ("1555f827-2570-419e-a21d-b48e01519e2f", "1-week"),
    ("eed3a028-dd71-4817-a713-b49100dae383", "1-week"),
    ("72bec82f-7cd7-4731-8695-b491018afaa0", "2-week"),
    ("1d7a4d29-bcde-451c-acb3-b49501776b95", "1-week"),
    ("2792a999-5908-4220-bce8-b48301070f12", "1-week"),
    ("9377b397-34cd-4739-af0b-b495017a430e", "1-week"),
    ("9377b397-34cd-4739-af0b-b495017a430e", "48-hour"),
    ("8174970a-2a7b-4525-837d-b4a200e5ceb6", "1-week"),
}

# Prior unpaid tracked clients for paid-since compare (from last successful report).
DEFAULT_PRIOR_UNPAID: list[dict[str, Any]] = [
    {
        "client_name": "Ashleigh Modlin",
        "client_id": "4a689e67-176c-4e5f-a294-b4a300e3fc33",
        "appointment_id": "8174970a-2a7b-4525-837d-b4a200e5ceb6",
        "milestone": "1-week",
        "invoice_id": "fdf39789-de86-49e4-8dbf-b4a300e58116",
        "invoice_number": "285",
        "outstanding": 225.0,
        "appointment_display": "Mon 8/17 3:30pm",
    }
]


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": os.environ["SHEPHERD_PUBLIC_KEY"],
        "X-Integration-Private-Key": os.environ["SHEPHERD_PRIVATE_KEY"],
        "X-Clinic-Id": os.environ["SHEPHERD_CLINIC_ID"],
    }


def shepherd_base() -> str:
    return os.environ["SHEPHERD_BASE_URL"].rstrip("/")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_paginated(endpoint: str, body: dict[str, Any] | None = None, rpp: int = 500) -> list[dict]:
    page = 1
    items: list[dict] = []
    base_body = dict(body or {})
    with httpx.Client(timeout=90.0) as client:
        while True:
            payload = {**base_body, "page": page, "rpp": min(rpp, 1000)}
            resp = client.post(
                f"{shepherd_base()}/{endpoint.lstrip('/')}",
                headers=shepherd_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("item") or data.get("items") or []
            if not isinstance(batch, list):
                break
            items.extend(batch)
            total = data.get("totalRecords") or data.get("total_records") or len(items)
            if len(items) >= total or not batch:
                break
            page += 1
    return items


def money(value: float) -> str:
    return f"${value:,.2f}"


def format_appt_local(start: datetime) -> tuple[str, str]:
    local = start.astimezone(TZ)
    day = local.strftime("%A")
    md = f"{local.month}/{local.day}"
    hour = local.strftime("%I").lstrip("0") or "0"
    minute = local.strftime("%M")
    ampm = local.strftime("%p").lower()
    time_str = f"{hour}:{minute}{ampm}"
    display = f"{local.strftime('%a')} {md} {time_str}"
    long_phrase = f"{day}, {md} at {time_str}"
    return display, long_phrase


def milestone_for(start: datetime, now: datetime) -> str | None:
    delta = start - now
    if delta.total_seconds() <= 0:
        return None
    if delta <= timedelta(hours=48):
        return "48-hour"
    if delta <= timedelta(days=7):
        return "1-week"
    if delta <= timedelta(days=14):
        return "2-week"
    return None


def client_display_name(client: dict[str, Any] | None) -> str:
    if not isinstance(client, dict):
        return "Unknown"
    first = str(client.get("firstName") or "").strip()
    last = str(client.get("lastName") or "").strip()
    name = " ".join(p for p in (first, last) if p).strip()
    if name:
        return name
    return str(client.get("name") or client.get("displayName") or "Unknown").strip() or "Unknown"


def first_name_only(client: dict[str, Any] | None) -> str:
    if not isinstance(client, dict):
        return "there"
    first = str(client.get("firstName") or "").strip()
    if first:
        return first
    full = client_display_name(client)
    return full.split()[0] if full and full != "Unknown" else "there"


def usable_email(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    email = value.strip()
    if not email or "@" not in email:
        return None
    if re.search(r"\s", email):
        return None
    return email


def is_deposit_style(invoice: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """Prefer correct non-sends: deposit invoices are small / travel+wellness style."""
    balance = float(invoice.get("balance") or 0)
    if balance <= 0:
        return False
    # Large multi-service AR (e.g. FUR) — exclude
    if balance >= 800 and len(items) >= 8:
        return False
    names = " ".join(str(i.get("name") or "").lower() for i in items)
    has_travel = "travel" in names
    has_exam = "exam" in names or "wellness" in names
    if len(items) <= 4 and (has_travel or has_exam or balance <= 500):
        return True
    if len(items) <= 2:
        return True
    # Prefer non-send for ambiguous large invoices
    if balance > 600 and len(items) > 5:
        return False
    return len(items) <= 6 and balance <= 500


def invoice_age_hours(invoice: dict[str, Any], now: datetime) -> float | None:
    created = parse_dt(invoice.get("dateCreated")) or parse_dt(invoice.get("dateIssued"))
    if not created:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600.0


def build_client_html(first: str, long_phrase: str, outstanding: float, signature_html: str) -> str:
    body = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222;">
<p>Hey {html.escape(first)},</p>
<p>This is a friendly reminder of your upcoming appointment with Whisker Waggin' Mobile Veterinary Care on {html.escape(long_phrase)}.</p>
<p>Our system indicates you have not yet paid the outstanding balance on your account in the amount of {html.escape(money(outstanding))} for the appointment deposit. For your convenience and to keep your visit running smoothly, we kindly ask that all appointment deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to your visit, we will unfortunately need to cancel the trip.</p>
<p>You can view and pay your open invoice through the Pet Portal linked below. Our team can also take payment directly over the phone at the number below. If you have any questions or need assistance, please reach out to <a href="mailto:admin@whiskerwagginwnc.com">admin@whiskerwagginwnc.com</a> and our team would be happy to help.</p>
<p><a href="{PET_PORTAL}">{PET_PORTAL}</a></p>
<p>We look forward to caring for your pet soon!</p>
</div>
<br>
{signature_html}"""
    return body


def build_staff_report_html(
    report_date: str,
    sent_rows: list[dict[str, Any]],
    paid_rows: list[dict[str, Any]],
    signature_html: str,
) -> str:
    def table(headers: list[str], rows: list[list[str]]) -> str:
        th = "".join(
            f'<th style="border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;">{html.escape(h)}</th>'
            for h in headers
        )
        if not rows:
            return f"<table style='border-collapse:collapse;margin:8px 0;'><tr>{th}</tr></table>"
        body_rows = []
        for row in rows:
            tds = "".join(
                f'<td style="border:1px solid #ccc;padding:6px 10px;vertical-align:top;">{html.escape(c)}</td>'
                for c in row
            )
            body_rows.append(f"<tr>{tds}</tr>")
        return (
            "<table style='border-collapse:collapse;margin:8px 0;'>"
            f"<tr>{th}</tr>{''.join(body_rows)}</table>"
        )

    section1: str
    if sent_rows:
        section1 = table(
            ["Client", "Email", "Appointment", "Milestone", "Outstanding", "Credit", "Invoice"],
            [
                [
                    r["client_name"],
                    r["email"],
                    r["appointment_display"],
                    r["milestone"],
                    money(r["outstanding"]),
                    money(r["credit"]),
                    f"#{r['invoice_number']}",
                ]
                for r in sent_rows
            ],
        )
    else:
        section1 = "<p><em>No qualifying clients this run.</em></p>"

    section2: str
    if paid_rows:
        section2 = table(
            ["Client", "Appointment", "Prior milestone", "Prior outstanding", "Status now", "Evidence"],
            [
                [
                    r["client_name"],
                    r["appointment_display"],
                    r["prior_milestone"],
                    money(r["prior_outstanding"]),
                    r["status_now"],
                    r["evidence"],
                ]
                for r in paid_rows
            ],
        )
    else:
        section2 = "<p><em>No previously reported clients have paid since the last run.</em></p>"

    return f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45;color:#222;">
<p><strong>[WWMVC] Outstanding Appointment Deposit Report — {html.escape(report_date)}</strong></p>
<p>LIVE mode run summary. Client deposit reminders are sent when all qualification rules pass.</p>
<h3 style="margin-bottom:4px;">Section 1 — Emails sent this run</h3>
{section1}
<h3 style="margin-bottom:4px;">Section 2 — Paid since last report</h3>
{section2}
</div>
<br>
{signature_html}"""


def get_composio():
    from composio import Composio

    return Composio(
        api_key=os.environ["COMPOSIO_API_KEY"],
        toolkit_versions={"gmail": GMAIL_TOOLKIT_VERSION},
    )


def gmail_execute(composio, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return composio.tools.execute(
        slug=slug,
        user_id=GMAIL_USER,
        version=GMAIL_TOOLKIT_VERSION,
        arguments=arguments,
    )


def load_signature(composio) -> str:
    result = gmail_execute(composio, "GMAIL_LIST_SEND_AS", {})
    data = result.get("data") or {}
    send_as = data.get("sendAs") or data.get("send_as") or []
    if not send_as:
        raise RuntimeError(f"GMAIL_LIST_SEND_AS returned no sendAs entries: {result!r}")
    # Prefer default / primary / NoReply
    chosen = None
    for entry in send_as:
        if entry.get("isPrimary") or entry.get("isDefault") or entry.get("isDefaultSendAs"):
            chosen = entry
            break
    if chosen is None:
        for entry in send_as:
            display = str(entry.get("displayName") or entry.get("sendAsEmail") or "").lower()
            if "noreply" in display or entry.get("sendAsEmail") == GMAIL_USER:
                chosen = entry
                break
    if chosen is None:
        chosen = send_as[0]
    signature = chosen.get("signature") or ""
    if not str(signature).strip():
        raise RuntimeError("Send-as signature missing/empty — refusing to invent a footer")
    return str(signature)


def extract_message_id(result: dict[str, Any]) -> str:
    data = result.get("data") or {}
    for key in ("id", "messageId", "message_id", "response_id"):
        if data.get(key):
            return str(data[key])
    response = data.get("response") or {}
    if isinstance(response, dict) and response.get("id"):
        return str(response["id"])
    return json.dumps(data)[:120]


def load_sent_milestones(path: str | None) -> set[tuple[str, str]]:
    sent = set(DEFAULT_SENT_MILESTONES)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2:
                    sent.add((parts[0], parts[1]))
    return sent


def qualify(now: datetime, sent_milestones: set[tuple[str, str]]) -> dict[str, Any]:
    date_from = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    appointments = fetch_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "embed": "client,appointmentStatus,appointmentType",
            "isDeleted": False,
        },
    )
    invoices = fetch_paginated(
        "open-api-invoices",
        {
            "embed": "invoiceStatus,invoiceItems,client",
            "isDeleted": False,
        },
    )
    payments = fetch_paginated(
        "open-api-payments",
        {"embed": "paymentMethod", "isDeleted": False},
    )

    # Latest credit proxy per client
    credit_by_client: dict[str, float] = {}
    latest_pay_dt: dict[str, datetime] = {}
    for p in payments:
        if p.get("isVoided") or p.get("voided") or p.get("isDeleted"):
            continue
        cid = str(p.get("clientId") or "")
        if not cid:
            continue
        pdt = parse_dt(p.get("paymentDate") or p.get("dateCreated"))
        if pdt is None:
            continue
        if cid in latest_pay_dt and pdt <= latest_pay_dt[cid]:
            continue
        latest_pay_dt[cid] = pdt
        bal = p.get("clientBalanceAfterPayment")
        try:
            credit_by_client[cid] = float(bal) if bal is not None else 0.0
        except (TypeError, ValueError):
            credit_by_client[cid] = 0.0

    # Active open invoices by client
    open_by_client: dict[str, list[dict[str, Any]]] = {}
    invoice_by_id: dict[str, dict[str, Any]] = {}
    for inv in invoices:
        invoice_by_id[str(inv.get("id") or "")] = inv
        status = str(((inv.get("invoiceStatus") or {}).get("name") or "")).strip()
        if status.lower() != "active":
            continue
        if inv.get("isDeleted") or inv.get("deleted"):
            continue
        bal = float(inv.get("balance") or 0)
        if bal <= 0:
            continue
        cid = str(inv.get("clientId") or ((inv.get("client") or {}).get("id") if isinstance(inv.get("client"), dict) else "") or "")
        if not cid:
            continue
        items = inv.get("invoiceItems") or []
        if not isinstance(items, list):
            items = []
        open_by_client.setdefault(cid, []).append({"invoice": inv, "items": items})

    upcoming: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    qualified: list[dict[str, Any]] = []

    for appt in appointments:
        if appt.get("isDeleted") or appt.get("deleted"):
            continue
        start = parse_dt(appt.get("startDate"))
        if not start:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if start <= now:
            continue
        status_name = str(((appt.get("appointmentStatus") or {}).get("name") or "")).strip()
        if status_name.lower() in TERMINAL_STATUSES:
            continue

        client = appt.get("client") if isinstance(appt.get("client"), dict) else None
        client_id = str(appt.get("clientId") or (client.get("id") if client else "") or "")
        if not client_id or client is None:
            continue

        display, long_phrase = format_appt_local(start)
        ms = milestone_for(start, now)
        row_base = {
            "appointment_id": str(appt.get("id")),
            "client_id": client_id,
            "client_name": client_display_name(client),
            "first_name": first_name_only(client),
            "email": usable_email(client.get("email")),
            "appointment_display": display,
            "appointment_long": long_phrase,
            "start": start.isoformat(),
            "status": status_name,
            "milestone": ms,
        }
        upcoming.append(row_base)

        if ms is None:
            exclusions.append({**row_base, "reason": "outside 14-day milestone window"})
            continue

        email = row_base["email"]
        if not email:
            exclusions.append({**row_base, "reason": "no usable email"})
            continue

        if (row_base["appointment_id"], ms) in sent_milestones:
            exclusions.append({**row_base, "reason": f"{ms} already sent for appointment"})
            continue

        credit = float(credit_by_client.get(client_id, 0.0))
        # Treat tiny float noise as zero
        if abs(credit) < 0.005:
            credit = 0.0
        if credit != 0.0:
            exclusions.append({**row_base, "reason": f"credit balance {money(credit)} != $0.00", "credit": credit})
            continue

        candidates = open_by_client.get(client_id) or []
        deposit_candidates = []
        for c in candidates:
            if is_deposit_style(c["invoice"], c["items"]):
                deposit_candidates.append(c)

        if not deposit_candidates:
            if candidates:
                exclusions.append(
                    {
                        **row_base,
                        "reason": "open Active invoice(s) but not deposit-style (prefer non-send)",
                        "open_invoices": [
                            f"#{(c['invoice'].get('invoiceNumber') or c['invoice'].get('number') or '?')} "
                            f"{money(float(c['invoice'].get('balance') or 0))} lines={len(c['items'])}"
                            for c in candidates
                        ],
                    }
                )
            else:
                exclusions.append({**row_base, "reason": "no Active outstanding invoice"})
            continue

        # Prefer the open deposit invoice closest in amount / most recent
        deposit_candidates.sort(
            key=lambda c: parse_dt(c["invoice"].get("dateCreated") or c["invoice"].get("dateIssued"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        chosen = deposit_candidates[0]
        inv = chosen["invoice"]
        age_h = invoice_age_hours(inv, now)
        if age_h is None:
            exclusions.append({**row_base, "reason": "invoice missing dateCreated/dateIssued"})
            continue
        if age_h <= 48.0:
            exclusions.append(
                {
                    **row_base,
                    "reason": f"invoice age {age_h:.1f}h ≤48h hold",
                    "invoice_number": str(inv.get("invoiceNumber") or inv.get("number") or ""),
                    "outstanding": float(inv.get("balance") or 0),
                    "invoice_age_hours": age_h,
                }
            )
            continue

        inv_num = str(inv.get("invoiceNumber") or inv.get("number") or "")
        outstanding = float(inv.get("balance") or 0)
        qualified.append(
            {
                **row_base,
                "credit": credit,
                "outstanding": outstanding,
                "invoice_id": str(inv.get("id") or ""),
                "invoice_number": inv_num,
                "invoice_age_hours": age_h,
                "item_names": [str(i.get("name") or "") for i in chosen["items"]],
            }
        )

    # Paid-since compare against prior unpaid
    paid_since: list[dict[str, Any]] = []
    still_unpaid_ids: set[str] = set()
    for prior in DEFAULT_PRIOR_UNPAID:
        cid = prior["client_id"]
        appt_id = prior["appointment_id"]
        inv_id = prior.get("invoice_id")
        credit = float(credit_by_client.get(cid, 0.0))
        if abs(credit) < 0.005:
            credit = 0.0

        inv = invoice_by_id.get(str(inv_id or ""), {})
        inv_status = str(((inv.get("invoiceStatus") or {}).get("name") or "")).strip() if inv else ""
        inv_balance = float(inv.get("balance") or 0) if inv else None

        # Find whether appointment still upcoming
        appt_still = next((a for a in upcoming if a["appointment_id"] == appt_id), None)
        appt_raw = next((a for a in appointments if str(a.get("id")) == appt_id), None)
        appt_status = str(((appt_raw.get("appointmentStatus") or {}).get("name") or "")).strip() if appt_raw else ""

        still_owes = False
        if inv and inv_status.lower() == "active" and (inv_balance or 0) > 0 and credit == 0.0:
            # Still qualifies as unpaid deposit (regardless of milestone window for tracking)
            if is_deposit_style(inv, inv.get("invoiceItems") or []):
                still_owes = True

        if still_owes:
            still_unpaid_ids.add(appt_id)
            continue

        # Determine status_now
        if credit > 0:
            status_now = "Paid / credit on account"
            evidence = f"Credit balance now {money(credit)}"
        elif inv and inv_balance is not None and inv_balance <= 0:
            status_now = "Balance cleared"
            evidence = f"Invoice #{prior.get('invoice_number')} status={inv_status or 'n/a'} balance={money(inv_balance)}"
        elif inv and inv_status.lower() != "active":
            status_now = "Balance cleared" if inv_status.lower() in {"checked out", "paid", "closed"} else "No longer Active open deposit"
            evidence = f"Invoice #{prior.get('invoice_number')} now {inv_status or 'missing'} balance={money(inv_balance or 0)}"
        elif not inv:
            status_now = "No longer Active open deposit"
            evidence = f"Invoice #{prior.get('invoice_number')} no longer present/open"
        elif appt_status.lower() in TERMINAL_STATUSES:
            status_now = "No longer Active open deposit"
            evidence = f"Appointment {appt_status}; deposit no longer open/unpaid"
        else:
            status_now = "No longer Active open deposit"
            evidence = "Previously tracked unpaid deposit no longer qualifies (paid/cleared)"

        paid_since.append(
            {
                "client_name": prior["client_name"],
                "appointment_display": prior.get("appointment_display") or (appt_still["appointment_display"] if appt_still else ""),
                "prior_milestone": prior["milestone"],
                "prior_outstanding": float(prior["outstanding"]),
                "status_now": status_now,
                "evidence": evidence,
                "appointment_id": appt_id,
                "client_id": cid,
            }
        )

    return {
        "now": now.isoformat(),
        "upcoming_count": len(upcoming),
        "qualified": qualified,
        "exclusions": exclusions,
        "paid_since": paid_since,
        "credit_by_client_sample": {k: credit_by_client[k] for k in list(credit_by_client)[:5]},
        "invoice_count": len(invoices),
        "payment_count": len(payments),
        "appointment_count": len(appointments),
    }


def send_failure_note(composio, note: str) -> None:
    try:
        gmail_execute(
            composio,
            "GMAIL_SEND_EMAIL",
            {
                "recipient_email": FAILURE_TO,
                "subject": "[WWMVC] Deposit reminder run failure",
                "body": note,
                "is_html": False,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to send failure note: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--sent-milestones-file", default="")
    parser.add_argument("--out", default="/tmp/deposit_reminder_result.json")
    args = parser.parse_args()
    if not args.qualify_only and not args.live:
        args.live = True  # default LIVE for this automation

    now = datetime.now(timezone.utc)
    sent = load_sent_milestones(args.sent_milestones_file or None)

    result: dict[str, Any] = {
        "mode": "qualify-only" if args.qualify_only else "live",
        "started_at": now.isoformat(),
    }

    try:
        q = qualify(now, sent)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "shepherd_failure"
        result["error"] = str(exc)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        if not args.qualify_only:
            try:
                composio = get_composio()
                send_failure_note(composio, f"Shepherd failure during deposit reminder run:\n{exc}")
            except Exception as gexc:  # noqa: BLE001
                result["failure_note_error"] = str(gexc)
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 1

    result.update(
        {
            "status": "qualified" if args.qualify_only else "running",
            "upcoming_count": q["upcoming_count"],
            "qualified_count": len(q["qualified"]),
            "qualified": q["qualified"],
            "exclusions": q["exclusions"],
            "paid_since": q["paid_since"],
            "appointment_count": q["appointment_count"],
            "invoice_count": q["invoice_count"],
            "payment_count": q["payment_count"],
        }
    )

    if args.qualify_only:
        result["status"] = "qualify-only-ok"
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 0

    # LIVE: load signature, send clients, send staff report
    sent_rows: list[dict[str, Any]] = []
    report_date = datetime.now(TZ).strftime("%B %-d, %Y").replace(" 0", " ")
    # %-d may not work on all platforms; normalize
    report_date = datetime.now(TZ).strftime("%B %d, %Y")
    report_date = re.sub(r" 0(\d),", r" \1,", report_date)
    # Better: Month D, YYYY without leading zero
    _local = datetime.now(TZ)
    report_date = f"{_local.strftime('%B')} {_local.day}, {_local.year}"

    try:
        composio = get_composio()
        signature_html = load_signature(composio)
        result["signature_chars"] = len(signature_html)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "gmail_signature_failure"
        result["error"] = str(exc)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        try:
            composio = get_composio()
            send_failure_note(
                composio,
                f"Gmail signature load failed during deposit reminder run:\n{exc}\n"
                "Staff report and client emails were NOT sent (no invented footer).",
            )
        except Exception as gexc:  # noqa: BLE001
            result["failure_note_error"] = str(gexc)
        print(json.dumps(result, indent=2, default=str))
        return 1

    client_send_errors: list[str] = []
    for row in q["qualified"]:
        body = build_client_html(
            row["first_name"],
            row["appointment_long"],
            float(row["outstanding"]),
            signature_html,
        )
        try:
            send_result = gmail_execute(
                composio,
                "GMAIL_SEND_EMAIL",
                {
                    "recipient_email": row["email"],
                    "subject": CLIENT_SUBJECT,
                    "body": body,
                    "is_html": True,
                },
            )
            mid = extract_message_id(send_result)
            sent_rows.append({**row, "gmail_message_id": mid, "send_ok": True})
        except Exception as exc:  # noqa: BLE001
            client_send_errors.append(f"{row['client_name']}: {exc}")
            sent_rows.append({**row, "send_ok": False, "error": str(exc)})

    result["sent"] = [r for r in sent_rows if r.get("send_ok")]
    result["send_failures"] = [r for r in sent_rows if not r.get("send_ok")]

    # Staff report lists ONLY successfully emailed clients
    ok_sent = [r for r in sent_rows if r.get("send_ok")]
    staff_html = build_staff_report_html(report_date, ok_sent, q["paid_since"], signature_html)
    staff_subject = f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}"
    try:
        staff_result = gmail_execute(
            composio,
            "GMAIL_SEND_EMAIL",
            {
                "recipient_email": STAFF_TO,
                "subject": staff_subject,
                "body": staff_html,
                "is_html": True,
            },
        )
        result["staff_report"] = {
            "ok": True,
            "subject": staff_subject,
            "message_id": extract_message_id(staff_result),
            "to": STAFF_TO,
        }
    except Exception as exc:  # noqa: BLE001
        result["staff_report"] = {"ok": False, "error": str(exc), "subject": staff_subject}
        send_failure_note(composio, f"Staff report send failed:\n{exc}")

    if client_send_errors:
        send_failure_note(
            composio,
            "Some client deposit reminder sends failed:\n" + "\n".join(client_send_errors),
        )

    # Build next unpaid tracking: prior still unpaid + newly sent
    next_unpaid: list[dict[str, Any]] = []
    paid_appt_ids = {p["appointment_id"] for p in q["paid_since"]}
    for prior in DEFAULT_PRIOR_UNPAID:
        if prior["appointment_id"] not in paid_appt_ids:
            # refresh if still in qualified/sent or still open
            next_unpaid.append(prior)
    for r in ok_sent:
        # upsert
        next_unpaid = [u for u in next_unpaid if u["appointment_id"] != r["appointment_id"]]
        next_unpaid.append(
            {
                "client_name": r["client_name"],
                "client_id": r["client_id"],
                "appointment_id": r["appointment_id"],
                "milestone": r["milestone"],
                "invoice_id": r["invoice_id"],
                "invoice_number": r["invoice_number"],
                "outstanding": r["outstanding"],
                "appointment_display": r["appointment_display"],
                "email": r["email"],
                "gmail_message_id": r.get("gmail_message_id"),
            }
        )
    # Also: if prior unpaid was NOT paid and NOT in ok_sent, keep as-is (already handled)
    # If prior unpaid paid, drop (handled via paid_appt_ids filter)

    # Re-check: for priors not in paid_since, keep; for ones that still qualify unpaid without new email, keep
    result["next_unpaid"] = next_unpaid
    result["status"] = "success" if result.get("staff_report", {}).get("ok") else "partial_failure"
    result["report_date"] = report_date

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
