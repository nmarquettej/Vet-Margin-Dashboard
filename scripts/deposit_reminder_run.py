#!/usr/bin/env python3
"""Whisker Waggin' appointment deposit reminder — LIVE / qualify-only runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/New_York")
PORTAL_URL = "https://petportal.vet/whisker-waggin-wnc/payment/0"
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
NOREPLY = "noreply@whiskerwagginwnc.com"
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAIL_TO = "nmarquettej@gmail.com"
GMAIL_VERSION = "20260721_00"
PAYMENT_URL = PORTAL_URL

# Exclusive current-window milestones
MILESTONE_48H = "48-hour"
MILESTONE_1W = "1-week"
MILESTONE_2W = "2-week"

TERMINAL_STATUSES = {
    "canceled",
    "cancelled",
    "checked out",
    "no show",
}

# Previously LIVE-sent milestones (appointment_id -> set of milestones)
# Loaded from --sent-milestones-json or empty
SENT_MILESTONES: dict[str, set[str]] = {}

# Previously tracked unpaid for paid-since compare
# list of dicts with appointment_id, client, milestone, outstanding, invoice_number, etc.
PRIOR_UNPAID: list[dict[str, Any]] = []


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": os.environ["SHEPHERD_PUBLIC_KEY"],
        "X-Integration-Private-Key": os.environ["SHEPHERD_PRIVATE_KEY"],
        "X-Clinic-Id": os.environ["SHEPHERD_CLINIC_ID"],
    }


def shepherd_base() -> str:
    return os.environ.get("SHEPHERD_BASE_URL", "").rstrip("/")


def shepherd_post(endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{shepherd_base()}/{endpoint.lstrip('/')}"
    r = requests.post(url, headers=shepherd_headers(), json=body or {}, timeout=90)
    r.raise_for_status()
    return r.json()


def shepherd_paginated(endpoint: str, body: dict[str, Any] | None = None, rpp: int = 500) -> list[dict]:
    page = 1
    items: list[dict] = []
    base = dict(body or {})
    while True:
        data = shepherd_post(endpoint, {**base, "page": page, "rpp": min(rpp, 1000)})
        batch = data.get("item") or data.get("items") or []
        if not isinstance(batch, list):
            break
        items.extend(batch)
        total = data.get("totalRecords") or data.get("total_records") or len(items)
        if len(items) >= total or not batch:
            break
        page += 1
    return items


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def money(n: float | int | None) -> str:
    return f"${(n or 0):,.2f}"


def first_name(full: str) -> str:
    parts = (full or "").strip().split()
    return parts[0] if parts else "there"


def format_appt_when(start: datetime) -> str:
    local = start.astimezone(TZ)
    day = local.strftime("%A")
    md = f"{local.month}/{local.day}"
    t = local.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day}, {md} at {t}"


def format_appt_short(start: datetime) -> str:
    local = start.astimezone(TZ)
    return f"{local.strftime('%a')} {local.month}/{local.day} {local.strftime('%I:%M%p').lstrip('0').lower()}"


def status_name(appt: dict) -> str:
    st = appt.get("appointmentStatus") or {}
    if isinstance(st, dict):
        return (st.get("name") or "").strip()
    return str(st or "").strip()


def is_upcoming(appt: dict, now: datetime) -> bool:
    if appt.get("isDeleted"):
        return False
    start = parse_dt(appt.get("startDate"))
    if not start or start <= now:
        return False
    name = status_name(appt).lower()
    if name in TERMINAL_STATUSES:
        return False
    return True


def current_milestone(start: datetime, now: datetime) -> str | None:
    delta = start - now
    if delta <= timedelta(0):
        return None
    if delta <= timedelta(hours=48):
        return MILESTONE_48H
    if delta <= timedelta(days=7):
        return MILESTONE_1W
    if delta <= timedelta(days=14):
        return MILESTONE_2W
    return None


def already_sent(appt_id: str, milestone: str) -> bool:
    return milestone in SENT_MILESTONES.get(appt_id, set())


def invoice_status_name(inv: dict) -> str:
    st = inv.get("invoiceStatus") or {}
    if isinstance(st, dict):
        return (st.get("name") or "").strip()
    return str(st or "").strip()


def invoice_age_hours(inv: dict, now: datetime) -> float | None:
    created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
    if not created:
        return None
    return (now - created).total_seconds() / 3600.0


def is_deposit_style(inv: dict, items_by_invoice: dict[str, list[dict]]) -> bool:
    """Prefer non-send for large multi-service AR. Deposit-style: few lines, wellness+travel or travel-only."""
    inv_id = str(inv.get("id") or "")
    items = items_by_invoice.get(inv_id) or inv.get("invoiceItems") or []
    if not isinstance(items, list):
        items = []
    # If no line items available, allow if balance looks like a deposit (< $500) — still prefer non-send for huge
    balance = float(inv.get("balance") or 0)
    if balance >= 800:
        # Large AR — only allow if clearly few deposit lines
        if len(items) > 4:
            return False
    if len(items) > 8:
        return False
    if len(items) == 0:
        # No items fetched: allow modest balances only
        return 0 < balance <= 500
    names = []
    for it in items:
        n = (it.get("name") or it.get("productName") or it.get("description") or "").lower()
        names.append(n)
    joined = " | ".join(names)
    # Travel-only or wellness+travel patterns
    has_travel = any("travel" in n for n in names)
    has_wellness = any("wellness" in n or "exam" in n for n in names)
    if len(items) <= 3 and (has_travel or has_wellness):
        return True
    if len(items) <= 2 and balance <= 500:
        return True
    # Explicit non-deposit: many product lines
    if len(items) >= 10:
        return False
    # Conservative: if few lines and modest balance, treat as deposit
    if len(items) <= 4 and balance <= 500:
        return True
    return False


def client_email_from(appt: dict, clients: dict[str, dict]) -> tuple[str, str]:
    """Return (full_name, email)."""
    client = appt.get("client") if isinstance(appt.get("client"), dict) else None
    cid = str(appt.get("clientId") or (client or {}).get("id") or "")
    if not client and cid and cid in clients:
        client = clients[cid]
    if not client:
        return "", ""
    name = (
        client.get("name")
        or " ".join(filter(None, [client.get("firstName"), client.get("lastName")])).strip()
        or ""
    )
    email = (client.get("email") or "").strip()
    return name, email


def usable_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    if email.lower().endswith("@placeholder.local"):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def build_client_html(first: str, when: str, outstanding: float, signature_html: str) -> str:
    bal = money(outstanding)
    paras = [
        f"Hey {first},",
        f"This is a friendly reminder of your upcoming appointment with Whisker Waggin' Mobile Veterinary Care on {when}.",
        f"Our system indicates you have not yet paid the outstanding balance on your account in the amount of {bal} for the appointment deposit. For your convenience and to keep your visit running smoothly, we kindly ask that all appointment deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to your visit, we will unfortunately need to cancel the trip.",
        "You can view and pay your open invoice through the Pet Portal linked below. Our team can also take payment directly over the phone at the number below. If you have any questions or need assistance, please reach out to admin@whiskerwagginwnc.com and our team would be happy to help.",
        f'<a href="{PAYMENT_URL}">{PAYMENT_URL}</a>',
        "We look forward to caring for your pet soon!",
    ]
    body = "".join(f"<p>{p}</p>" for p in paras)
    return body + "<br>" + signature_html


def build_staff_report_html(
    sent_rows: list[dict],
    paid_rows: list[dict],
    report_date: str,
    signature_html: str,
) -> str:
    parts = [
        f"<p><b>[WWMVC] Outstanding Appointment Deposit Report — {report_date}</b></p>",
        "<h3>Section 1 — Emails sent this run</h3>",
    ]
    if not sent_rows:
        parts.append("<p>No qualifying clients this run.</p>")
    else:
        parts.append(
            '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">'
            "<tr><th>Client</th><th>Email</th><th>Appointment</th><th>Milestone</th>"
            "<th>Outstanding</th><th>Credit</th><th>Invoice</th></tr>"
        )
        for r in sent_rows:
            parts.append(
                "<tr>"
                f"<td>{r['client']}</td>"
                f"<td>{r['email']}</td>"
                f"<td>{r['appointment']}</td>"
                f"<td>{r['milestone']}</td>"
                f"<td>{r['outstanding']}</td>"
                f"<td>{r['credit']}</td>"
                f"<td>{r['invoice']}</td>"
                "</tr>"
            )
        parts.append("</table>")

    parts.append("<h3>Section 2 — Paid since last report</h3>")
    if not paid_rows:
        parts.append("<p>No previously reported clients have paid since the last run.</p>")
    else:
        parts.append(
            '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">'
            "<tr><th>Client</th><th>Appointment</th><th>Prior milestone</th>"
            "<th>Prior outstanding</th><th>Status now</th><th>Evidence</th></tr>"
        )
        for r in paid_rows:
            parts.append(
                "<tr>"
                f"<td>{r['client']}</td>"
                f"<td>{r['appointment']}</td>"
                f"<td>{r['prior_milestone']}</td>"
                f"<td>{r['prior_outstanding']}</td>"
                f"<td>{r['status_now']}</td>"
                f"<td>{r['evidence']}</td>"
                "</tr>"
            )
        parts.append("</table>")

    return "".join(parts) + "<br>" + signature_html


def get_composio():
    from composio import Composio

    return Composio(api_key=os.environ["COMPOSIO_API_KEY"], toolkit_versions={"gmail": GMAIL_VERSION})


def load_signature(c) -> str:
    r = c.tools.execute(
        slug="GMAIL_LIST_SEND_AS",
        user_id=NOREPLY,
        version=GMAIL_VERSION,
        arguments={},
    )
    data = r.get("data") or {}
    send_as = data.get("sendAs") or []
    if not send_as:
        raise RuntimeError("GMAIL_LIST_SEND_AS returned no sendAs entries")
    # Prefer default / primary noreply
    chosen = None
    for sa in send_as:
        if sa.get("isDefault") or sa.get("isPrimary"):
            chosen = sa
            break
        if (sa.get("sendAsEmail") or "").lower() == NOREPLY.lower():
            chosen = sa
    if not chosen:
        chosen = send_as[0]
    sig = chosen.get("signature") or ""
    if not sig.strip():
        raise RuntimeError("NoReply send-as signature missing/empty — refuse to invent footer")
    return sig


def gmail_send(c, to: str, subject: str, body_html: str) -> dict:
    return c.tools.execute(
        slug="GMAIL_SEND_EMAIL",
        user_id=NOREPLY,
        version=GMAIL_VERSION,
        arguments={
            "recipient_email": to,
            "subject": subject,
            "body": body_html,
            "is_html": True,
        },
    )


def extract_message_id(result: dict) -> str:
    data = result.get("data") or result
    for key in ("id", "messageId", "message_id", "response_id"):
        if data.get(key):
            return str(data[key])
    resp = data.get("response") or {}
    if isinstance(resp, dict) and resp.get("id"):
        return str(resp["id"])
    return json.dumps(data)[:80]


def credit_balances(payments: list[dict]) -> dict[str, float]:
    """Latest non-voided payment's clientBalanceAfterPayment per clientId."""
    best: dict[str, tuple[datetime, float]] = {}
    for p in payments:
        if p.get("isDeleted") or p.get("isVoided") or p.get("voided"):
            continue
        cid = str(p.get("clientId") or "")
        if not cid:
            continue
        bal = p.get("clientBalanceAfterPayment")
        if bal is None:
            continue
        pdt = parse_dt(p.get("paymentDate")) or parse_dt(p.get("dateCreated")) or datetime.min.replace(
            tzinfo=timezone.utc
        )
        prev = best.get(cid)
        if not prev or pdt >= prev[0]:
            best[cid] = (pdt, float(bal))
    return {cid: bal for cid, (_, bal) in best.items()}


def qualify(now: datetime) -> dict[str, Any]:
    date_from = (now - timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    appointments = shepherd_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "embed": "client,appointmentStatus,appointmentType",
            "isDeleted": False,
        },
    )
    invoices = shepherd_paginated(
        "open-api-invoices",
        {"embed": "invoiceStatus,invoiceItems", "isDeleted": False},
    )
    payments = shepherd_paginated("open-api-payments", {"isDeleted": False})

    credits = credit_balances(payments)

    # Index active open invoices by client
    items_by_inv: dict[str, list] = {}
    for inv in invoices:
        inv_id = str(inv.get("id") or "")
        items = inv.get("invoiceItems")
        if isinstance(items, list):
            items_by_inv[inv_id] = items

    active_open_by_client: dict[str, list[dict]] = {}
    for inv in invoices:
        if inv.get("isDeleted"):
            continue
        if invoice_status_name(inv).lower() != "active":
            continue
        bal = float(inv.get("balance") or 0)
        if bal <= 0:
            continue
        cid = str(inv.get("clientId") or "")
        if not cid:
            continue
        active_open_by_client.setdefault(cid, []).append(inv)

    # Clients for appointments missing embed email
    need_ids = []
    for a in appointments:
        if not is_upcoming(a, now):
            continue
        client = a.get("client") if isinstance(a.get("client"), dict) else None
        email = (client or {}).get("email") if client else None
        cid = str(a.get("clientId") or (client or {}).get("id") or "")
        if cid and not email:
            need_ids.append(cid)
    clients: dict[str, dict] = {}
    if need_ids:
        unique = list(dict.fromkeys(need_ids))
        for i in range(0, len(unique), 100):
            chunk = unique[i : i + 100]
            batch = shepherd_paginated("open-api-clients", {"ids": ",".join(chunk)})
            for c in batch:
                clients[str(c.get("id"))] = c

    upcoming = [a for a in appointments if is_upcoming(a, now)]
    # Within 14d for reporting exclusions
    within_14 = []
    qualified = []
    exclusions = []

    for appt in upcoming:
        start = parse_dt(appt.get("startDate"))
        assert start
        ms = current_milestone(start, now)
        name, email = client_email_from(appt, clients)
        cid = str(appt.get("clientId") or "")
        appt_id = str(appt.get("id") or "")
        short = format_appt_short(start)
        row_base = {
            "appointment_id": appt_id,
            "client_id": cid,
            "client": name or "(unknown)",
            "email": email,
            "appointment": short,
            "start": start.isoformat(),
            "status": status_name(appt),
            "milestone": ms,
        }
        if ms is None:
            continue  # outside 14d window — not relevant for this run's reminders
        within_14.append(row_base)

        if not usable_email(email):
            exclusions.append({**row_base, "reason": "no usable email"})
            continue
        credit = float(credits.get(cid, 0.0))
        if abs(credit) > 0.001:
            exclusions.append({**row_base, "reason": f"credit {money(credit)} ≠ $0.00", "credit": credit})
            continue
        opens = active_open_by_client.get(cid) or []
        deposit_invs = [inv for inv in opens if is_deposit_style(inv, items_by_inv)]
        if not deposit_invs:
            if opens:
                exclusions.append(
                    {
                        **row_base,
                        "reason": "Active open invoice(s) but not deposit-style",
                        "invoices": [
                            f"#{i.get('invoiceNumber') or i.get('number')} {money(float(i.get('balance') or 0))}"
                            for i in opens
                        ],
                    }
                )
            else:
                exclusions.append({**row_base, "reason": "no Active outstanding deposit invoice"})
            continue
        # Prefer the open deposit invoice (lowest number / oldest)
        deposit_invs.sort(key=lambda i: parse_dt(i.get("dateCreated")) or datetime.min.replace(tzinfo=timezone.utc))
        inv = deposit_invs[0]
        age_h = invoice_age_hours(inv, now)
        inv_num = inv.get("invoiceNumber") or inv.get("number") or inv.get("id")
        bal = float(inv.get("balance") or 0)
        if age_h is None or age_h <= 48:
            exclusions.append(
                {
                    **row_base,
                    "reason": f"invoice age {age_h:.1f}h ≤ 48h" if age_h is not None else "invoice age unknown",
                    "invoice": f"#{inv_num} {money(bal)}",
                    "invoice_age_h": age_h,
                }
            )
            continue
        if already_sent(appt_id, ms):
            exclusions.append({**row_base, "reason": f"milestone {ms} already sent", "invoice": f"#{inv_num} {money(bal)}"})
            continue

        qualified.append(
            {
                **row_base,
                "first_name": first_name(name),
                "when": format_appt_when(start),
                "outstanding": bal,
                "credit": credit,
                "invoice_number": inv_num,
                "invoice_id": str(inv.get("id")),
                "invoice_age_h": age_h,
                "invoice_label": f"#{inv_num} {money(bal)}",
            }
        )

    # One email per deposit invoice (same client + same open deposit across
    # multiple same-day appointment slots must not get duplicate reminders).
    # Prefer earliest appointment; mark later slots as exclusions.
    qualified.sort(key=lambda r: r["start"])
    seen_invoices: dict[str, dict] = {}
    deduped: list[dict] = []
    for row in qualified:
        inv_key = row.get("invoice_id") or f"{row.get('client_id')}:{row.get('invoice_number')}"
        if inv_key in seen_invoices:
            primary = seen_invoices[inv_key]
            primary.setdefault("sibling_appointment_ids", []).append(row["appointment_id"])
            exclusions.append(
                {
                    **{k: row[k] for k in (
                        "appointment_id", "client_id", "client", "email",
                        "appointment", "start", "status", "milestone",
                    )},
                    "reason": f"duplicate deposit invoice #{row.get('invoice_number')} (already queued for earlier appointment)",
                    "invoice": row.get("invoice_label"),
                }
            )
            continue
        row = {**row, "sibling_appointment_ids": []}
        seen_invoices[inv_key] = row
        deduped.append(row)
    qualified = deduped

    # Paid since last report
    paid_since = []
    for prior in PRIOR_UNPAID:
        pid = str(prior.get("appointment_id") or "")
        pcid = str(prior.get("client_id") or "")
        pname = prior.get("client") or ""
        prior_ms = prior.get("milestone") or ""
        prior_out = prior.get("outstanding_label") or prior.get("outstanding") or ""
        prior_appt = prior.get("appointment") or ""
        prior_inv = str(prior.get("invoice_number") or "")

        credit = float(credits.get(pcid, 0.0)) if pcid else 0.0
        opens = active_open_by_client.get(pcid) or []
        deposit_opens = [inv for inv in opens if is_deposit_style(inv, items_by_inv)]

        # Find matching appointment status
        matching = [a for a in appointments if str(a.get("id")) == pid]
        appt_gone_or_terminal = False
        if matching:
            a = matching[0]
            st = status_name(a).lower()
            if st in TERMINAL_STATUSES or a.get("isDeleted"):
                appt_gone_or_terminal = True
        else:
            # Appointment not in fetch — may be past/canceled
            appt_gone_or_terminal = True

        status_now = None
        evidence = None
        if credit > 0.001:
            status_now = "Paid / credit on account"
            evidence = f"Credit balance now {money(credit)}"
            if prior_inv:
                evidence += f"; prior reminder for #{prior_inv}"
        elif not deposit_opens:
            # Check if prior invoice cleared
            prior_inv_obj = None
            for inv in invoices:
                num = str(inv.get("invoiceNumber") or inv.get("number") or "")
                if prior_inv and num == str(prior_inv):
                    prior_inv_obj = inv
                    break
            if prior_inv_obj:
                st = invoice_status_name(prior_inv_obj)
                bal = float(prior_inv_obj.get("balance") or 0)
                if bal <= 0 or st.lower() in ("checked out", "paid", "void", "voided"):
                    status_now = "Balance cleared"
                    evidence = f"Invoice #{prior_inv} {st} {money(bal)}"
                else:
                    status_now = "No longer Active open deposit"
                    evidence = f"Invoice #{prior_inv} status {st} balance {money(bal)}"
            else:
                status_now = "No longer Active open deposit"
                evidence = "No Active open deposit invoice found"
            if appt_gone_or_terminal and matching:
                evidence += f"; appointment {status_name(matching[0])}"
            elif appt_gone_or_terminal:
                evidence += "; appointment no longer upcoming"
        elif appt_gone_or_terminal and matching and status_name(matching[0]).lower() in TERMINAL_STATUSES:
            status_now = "No longer Active open deposit"
            evidence = f"Appointment {status_name(matching[0])}; deposit tracking cleared"
        # else still unpaid — not paid-since

        if status_now:
            paid_since.append(
                {
                    "client": pname,
                    "appointment": prior_appt,
                    "prior_milestone": prior_ms,
                    "prior_outstanding": prior_out if isinstance(prior_out, str) else money(float(prior_out or 0)),
                    "status_now": status_now,
                    "evidence": evidence,
                    "appointment_id": pid,
                    "client_id": pcid,
                }
            )

    return {
        "now": now.isoformat(),
        "qualified": qualified,
        "exclusions": exclusions,
        "within_14": within_14,
        "paid_since": paid_since,
        "counts": {
            "appointments_fetched": len(appointments),
            "upcoming": len(upcoming),
            "within_14": len(within_14),
            "qualified": len(qualified),
            "exclusions": len(exclusions),
            "paid_since": len(paid_since),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--sent-milestones-json", default="")
    parser.add_argument("--prior-unpaid-json", default="")
    parser.add_argument("--out", default="/tmp/deposit_reminder_result.json")
    args = parser.parse_args()

    global SENT_MILESTONES, PRIOR_UNPAID
    if args.sent_milestones_json:
        raw = json.loads(args.sent_milestones_json)
        SENT_MILESTONES = {k: set(v) for k, v in raw.items()}
    if args.prior_unpaid_json:
        PRIOR_UNPAID = json.loads(args.prior_unpaid_json)

    now = datetime.now(timezone.utc)
    mode = "LIVE" if args.live else ("QUALIFY" if args.qualify_only else "QUALIFY")

    result: dict[str, Any] = {"mode": mode, "ok": False}

    try:
        q = qualify(now)
        result.update(q)
        result["ok"] = True
    except Exception as e:
        result["error"] = f"Shepherd failure: {e}"
        result["ok"] = False
        if args.live or not args.qualify_only:
            try:
                c = get_composio()
                sig = load_signature(c)
                body = f"<p>Deposit reminder run failed (Shepherd): {e}</p><br>{sig}"
                gmail_send(c, FAIL_TO, "[WWMVC] Deposit reminder FAILURE", body)
                result["failure_note_sent"] = True
            except Exception as ge:
                result["failure_note_error"] = str(ge)
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 1

    if args.qualify_only and not args.live:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps({"counts": result["counts"], "qualified": result["qualified"], "paid_since": result["paid_since"], "exclusions": result["exclusions"]}, indent=2, default=str))
        return 0

    # LIVE send
    sent_rows = []
    send_errors = []
    try:
        c = get_composio()
        signature_html = load_signature(c)
        result["signature_chars"] = len(signature_html)
    except Exception as e:
        result["error"] = f"Gmail signature failure: {e}"
        result["ok"] = False
        try:
            c = get_composio()
            # Cannot append signature — still try bare failure note? Instructions say fail Gmail step
            # and note in staff report. Try send failure note without inventing footer — use plain if needed.
            gmail_send(
                c,
                FAIL_TO,
                "[WWMVC] Deposit reminder FAILURE",
                f"<p>Deposit reminder run failed (Gmail signature): {e}</p>",
            )
            result["failure_note_sent"] = True
        except Exception as ge:
            result["failure_note_error"] = str(ge)
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 1

    for qrow in result["qualified"]:
        try:
            html = build_client_html(
                qrow["first_name"],
                qrow["when"],
                qrow["outstanding"],
                signature_html,
            )
            resp = gmail_send(c, qrow["email"], CLIENT_SUBJECT, html)
            mid = extract_message_id(resp)
            sent_rows.append(
                {
                    "client": qrow["client"],
                    "email": qrow["email"],
                    "appointment": qrow["appointment"],
                    "milestone": qrow["milestone"],
                    "outstanding": money(qrow["outstanding"]),
                    "credit": money(qrow["credit"]),
                    "invoice": qrow["invoice_label"],
                    "appointment_id": qrow["appointment_id"],
                    "client_id": qrow["client_id"],
                    "message_id": mid,
                    "outstanding_raw": qrow["outstanding"],
                    "invoice_number": qrow["invoice_number"],
                    "sibling_appointment_ids": qrow.get("sibling_appointment_ids") or [],
                }
            )
        except Exception as e:
            send_errors.append({"client": qrow.get("client"), "error": str(e)})

    result["sent"] = sent_rows
    result["send_errors"] = send_errors

    # Staff report date in Eastern
    report_date = datetime.now(TZ).strftime("%B %-d, %Y").replace(" 0", " ")
    # %-d may not work on all platforms
    report_date = datetime.now(TZ).strftime("%B %d, %Y").replace(" 0", " ")

    paid_rows = [
        {
            "client": p["client"],
            "appointment": p["appointment"],
            "prior_milestone": p["prior_milestone"],
            "prior_outstanding": p["prior_outstanding"],
            "status_now": p["status_now"],
            "evidence": p["evidence"],
        }
        for p in result["paid_since"]
    ]

    staff_subject = f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}"
    staff_html = build_staff_report_html(sent_rows, paid_rows, report_date, signature_html)

    try:
        staff_resp = gmail_send(c, STAFF_TO, staff_subject, staff_html)
        result["staff_report"] = {
            "to": STAFF_TO,
            "subject": staff_subject,
            "message_id": extract_message_id(staff_resp),
            "ok": True,
        }
    except Exception as e:
        result["staff_report"] = {"ok": False, "error": str(e)}
        try:
            gmail_send(
                c,
                FAIL_TO,
                "[WWMVC] Deposit reminder FAILURE",
                f"<p>Staff report send failed: {e}</p><br>{signature_html}",
            )
            result["failure_note_sent"] = True
        except Exception as ge:
            result["failure_note_error"] = str(ge)

    # Currently tracked unpaid = those we just sent (still unpaid) + prior unpaid not in paid_since
    paid_ids = {p["appointment_id"] for p in result["paid_since"]}
    still_unpaid = []
    for p in PRIOR_UNPAID:
        if str(p.get("appointment_id")) not in paid_ids:
            # Still tracked if still qualifies as unpaid — keep if not paid
            still_unpaid.append(p)
    for s in sent_rows:
        still_unpaid.append(
            {
                "appointment_id": s["appointment_id"],
                "client_id": s["client_id"],
                "client": s["client"],
                "email": s["email"],
                "appointment": s["appointment"],
                "milestone": s["milestone"],
                "outstanding": s["outstanding_raw"],
                "outstanding_label": s["outstanding"],
                "invoice_number": s["invoice_number"],
            }
        )
    # Dedupe by appointment_id (latest wins)
    by_id = {}
    for u in still_unpaid:
        by_id[str(u["appointment_id"])] = u
    result["tracked_unpaid"] = list(by_id.values())

    # appointment_id -> milestones to record as sent (includes sibling slots sharing deposit)
    milestones_to_mark: dict[str, list[str]] = {}
    for s in sent_rows:
        ms = s["milestone"]
        ids = [s["appointment_id"]] + list(s.get("sibling_appointment_ids") or [])
        for aid in ids:
            milestones_to_mark.setdefault(str(aid), [])
            if ms not in milestones_to_mark[str(aid)]:
                milestones_to_mark[str(aid)].append(ms)
    result["milestones_to_mark"] = milestones_to_mark

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, default=str)

    summary = {
        "ok": result.get("ok"),
        "counts": result.get("counts"),
        "sent_count": len(sent_rows),
        "sent": sent_rows,
        "paid_since": result.get("paid_since"),
        "staff_report": result.get("staff_report"),
        "exclusions": result.get("exclusions"),
        "tracked_unpaid": result.get("tracked_unpaid"),
        "milestones_to_mark": milestones_to_mark,
        "send_errors": send_errors,
        "signature_chars": result.get("signature_chars"),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if result.get("staff_report", {}).get("ok") and not send_errors else 1


if __name__ == "__main__":
    sys.exit(main())
