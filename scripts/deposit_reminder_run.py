#!/usr/bin/env python3
"""Whisker Waggin' appointment deposit reminder — LIVE or qualify-only.

Usage:
  python scripts/deposit_reminder_run.py --qualify-only
  python scripts/deposit_reminder_run.py --send
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("America/New_York")
GMAIL_USER = "noreply@whiskerwagginwnc.com"
GMAIL_VERSION = "20260721_00"
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAILURE_TO = "nmarquettej@gmail.com"
PET_PORTAL = "https://petportal.vet/whisker-waggin-wnc/payment/0"

# Previously LIVE-sent milestones (appointment_id|milestone). Updated in-memory file after run.
SENT_MILESTONES: set[tuple[str, str]] = {
    ("1555f827-2570-419e-a21d-b48e01519e2f", "1-week"),
    ("eed3a028-dd71-4817-a713-b49100dae383", "2-week"),
    ("eed3a028-dd71-4817-a713-b49100dae383", "1-week"),
    ("72bec82f-7cd7-4731-8695-b491018afaa0", "2-week"),
    ("1d7a4d29-bcde-451c-acb3-b49501776b95", "1-week"),
    ("2792a999-5908-4220-bce8-b48301070f12", "1-week"),
    ("9377b397-34cd-4739-af0b-b495017a430e", "1-week"),
    ("9377b397-34cd-4739-af0b-b495017a430e", "48-hour"),
    ("8174970a-2a7b-4525-837d-b4a200e5ceb6", "1-week"),
    ("3eefb3be-5c2c-4284-9f61-b48d00dba095", "2-week"),
    ("c5157f6f-d9b9-434a-af39-b4a500dede04", "2-week"),
    ("345a3404-6cbe-470b-bfad-b4a601681146", "2-week"),
    ("66a63a00-cead-42dd-ac2b-b4a900db134d", "2-week"),
    ("e88a5dd0-4c20-4887-b631-b4b2010a52f6", "48-hour"),
}

# Currently tracked unpaid for paid-since compare (from drafted-milestones.md).
TRACKED_UNPAID: list[dict] = []

CANCEL_STATUSES = {
    "canceled",
    "cancelled",
    "checked out",
    "no show",
    "deleted",
}


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": os.environ["SHEPHERD_PUBLIC_KEY"],
        "X-Integration-Private-Key": os.environ["SHEPHERD_PRIVATE_KEY"],
        "X-Clinic-Id": os.environ["SHEPHERD_CLINIC_ID"],
    }


def shepherd_base() -> str:
    return os.environ.get("SHEPHERD_BASE_URL", "").rstrip("/")


def shepherd_post(endpoint: str, body: dict | None = None) -> dict:
    url = f"{shepherd_base()}/{endpoint.lstrip('/')}"
    with httpx.Client(timeout=90.0) as client:
        r = client.post(url, headers=shepherd_headers(), json=body or {})
        r.raise_for_status()
        return r.json()


def shepherd_paginated(endpoint: str, body: dict | None = None, rpp: int = 500) -> list[dict]:
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


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def usable_email(email: str | None) -> bool:
    if not email:
        return False
    e = email.strip()
    if not e or "@" not in e:
        return False
    if e.lower() in {"noreply@", "null", "none", "n/a"}:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e))


def first_name(full: str | None) -> str:
    if not full:
        return "there"
    parts = full.strip().split()
    return parts[0] if parts else "there"


def appointment_local(start: datetime) -> datetime:
    return start.astimezone(TZ)


def format_appt_phrase(start: datetime) -> str:
    local = appointment_local(start)
    day = local.strftime("%A")
    date = f"{local.month}/{local.day}"
    hour = local.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day}, {date} at {hour}"


def format_appt_short(start: datetime) -> str:
    local = appointment_local(start)
    day = local.strftime("%a")
    date = f"{local.month}/{local.day}"
    hour = local.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day} {date} {hour}"


def current_milestone(start: datetime, now: datetime) -> str | None:
    """Exclusive current-window tiers: 48h / 1-week / 2-week."""
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
    if name in CANCEL_STATUSES:
        return False
    return True


def invoice_status_name(inv: dict) -> str:
    st = inv.get("invoiceStatus") or {}
    if isinstance(st, dict):
        return (st.get("name") or "").strip()
    return str(st or "").strip()


def invoice_age_ok(inv: dict, now: datetime) -> bool:
    created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
    if not created:
        return False
    return (now - created) > timedelta(hours=48)


def line_text(item: dict) -> str:
    bits = [
        str(item.get("name") or ""),
        str(item.get("description") or ""),
        str((item.get("product") or {}).get("name") or "") if isinstance(item.get("product"), dict) else "",
        str(item.get("productName") or ""),
    ]
    return " ".join(bits).lower()


def is_deposit_style(inv: dict, items: list[dict]) -> bool:
    """Prefer correct non-sends: exam/wellness + travel, or travel-only ≤$150."""
    bal = money(inv.get("balance"))
    if bal <= 0:
        return False
    if not items:
        # Without lines, only allow small balances as possible travel-only deposits.
        return bal <= 150

    texts = [line_text(i) for i in items if not i.get("isDeleted")]
    texts = [t for t in texts if t.strip()]
    if not texts:
        return bal <= 150

    travel = [t for t in texts if "travel" in t or "trip" in t or "mobile fee" in t]
    exam = [
        t
        for t in texts
        if any(
            k in t
            for k in (
                "wellness",
                "exam",
                "office visit",
                "appointment deposit",
                "deposit",
            )
        )
    ]
    # Exclude clear non-deposit categories
    bad_keywords = (
        "vaccine",
        "vaccination",
        "felv",
        "fvrcp",
        "rabies",
        "lab",
        "blood",
        "chem",
        "dental",
        "surgery",
        "euthanasia",
        "cremation",
        "wipe",
        "shampoo",
        "medication",
        "rx ",
        "prescription",
        "flea",
        "heartworm",
        "douxo",
    )
    if any(any(b in t for b in bad_keywords) for t in texts):
        # Allow if still clearly exam+travel and bad is incidental? Prefer non-send.
        return False

    if len(texts) == 1 and travel and bal <= 150:
        return True
    if travel and exam and len(texts) <= 4:
        return True
    # Known pattern: Wellness Exam + Travel Fee only (2 lines)
    if len(texts) == 2 and travel and exam:
        return True
    return False


def reminder_html(first: str, appt_phrase: str, outstanding: float) -> str:
    bal = f"{outstanding:,.2f}"
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222;">
<p>Hey {first},</p>
<p>This is a friendly reminder of your upcoming appointment with Whisker Waggin' Mobile Veterinary Care on {appt_phrase}.</p>
<p>Our system indicates you have not yet paid the outstanding balance on your account in the amount of ${bal} for the appointment deposit. For your convenience and to keep your visit running smoothly, we kindly ask that all appointment deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to your visit, we will unfortunately need to cancel the trip.</p>
<p>You can view and pay your open invoice through the Pet Portal linked below. Our team can also take payment directly over the phone at the number below. If you have any questions or need assistance, please reach out to <a href="mailto:admin@whiskerwagginwnc.com">admin@whiskerwagginwnc.com</a> and our team would be happy to help.</p>
<p><a href="{PET_PORTAL}">{PET_PORTAL}</a></p>
<p>We look forward to caring for your pet soon!</p>
</div>"""


def staff_report_html(
    report_date: str,
    sent_rows: list[dict],
    paid_rows: list[dict],
) -> str:
    def table(headers: list[str], rows: list[list[str]]) -> str:
        th = "".join(
            f'<th style="border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;">{h}</th>'
            for h in headers
        )
        body = ""
        for row in rows:
            tds = "".join(
                f'<td style="border:1px solid #ccc;padding:6px 10px;vertical-align:top;">{c}</td>'
                for c in row
            )
            body += f"<tr>{tds}</tr>"
        return (
            '<table style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:13px;margin:8px 0 16px;"><thead><tr>{th}</tr></thead>'
            f"<tbody>{body}</tbody></table>"
        )

    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">',
        f"<p><strong>Outstanding Appointment Deposit Report — {report_date}</strong></p>",
        "<h3 style='margin:16px 0 6px;'>Section 1 — Emails sent this run</h3>",
    ]
    if not sent_rows:
        parts.append("<p>No qualifying clients this run.</p>")
    else:
        parts.append(
            table(
                ["Client", "Email", "Appointment", "Milestone", "Outstanding", "Credit", "Invoice"],
                [
                    [
                        r["client"],
                        r["email"],
                        r["appointment"],
                        r["milestone"],
                        r["outstanding"],
                        r["credit"],
                        r["invoice"],
                    ]
                    for r in sent_rows
                ],
            )
        )

    parts.append("<h3 style='margin:16px 0 6px;'>Section 2 — Paid since last report</h3>")
    if not paid_rows:
        parts.append("<p>No previously reported clients have paid since the last run.</p>")
    else:
        parts.append(
            table(
                ["Client", "Appointment", "Prior milestone", "Prior outstanding", "Status now", "Evidence"],
                [
                    [
                        r["client"],
                        r["appointment"],
                        r["prior_milestone"],
                        r["prior_outstanding"],
                        r["status_now"],
                        r["evidence"],
                    ]
                    for r in paid_rows
                ],
            )
        )
    parts.append("</div>")
    return "\n".join(parts)


def get_composio():
    from composio import Composio

    return Composio(
        api_key=os.environ["COMPOSIO_API_KEY"],
        toolkit_versions={"gmail": GMAIL_VERSION},
    )


def load_signature(c) -> str:
    sig = c.tools.execute(
        slug="GMAIL_LIST_SEND_AS",
        user_id=GMAIL_USER,
        version=GMAIL_VERSION,
        arguments={},
    )
    send_as = (sig.get("data") or {}).get("sendAs") or []
    if not send_as:
        raise RuntimeError("GMAIL_LIST_SEND_AS returned no sendAs entries")
    # Prefer primary / default; else first with signature
    primary = None
    for entry in send_as:
        if entry.get("isPrimary") or entry.get("isDefault"):
            primary = entry
            break
    if primary is None:
        primary = send_as[0]
    signature = (primary.get("signature") or "").strip()
    if not signature:
        raise RuntimeError("NoReply send-as signature empty / missing")
    return signature


def gmail_send(c, to: str, subject: str, html_body: str) -> str:
    r = c.tools.execute(
        slug="GMAIL_SEND_EMAIL",
        user_id=GMAIL_USER,
        version=GMAIL_VERSION,
        arguments={
            "recipient_email": to,
            "subject": subject,
            "body": html_body,
            "is_html": True,
        },
    )
    data = r.get("data") or {}
    mid = data.get("id") or data.get("messageId") or data.get("response_id") or ""
    if r.get("successful") is False or (r.get("error") and not mid):
        raise RuntimeError(f"Gmail send failed to {to}: {r}")
    return str(mid)


def try_failure_note(message: str) -> None:
    try:
        c = get_composio()
        sig = load_signature(c)
        body = f"<div><p>{message}</p></div><br>{sig}"
        gmail_send(
            c,
            FAILURE_TO,
            "[WWMVC] Deposit Reminder Run Failure",
            body,
        )
        print("Failure note sent to", FAILURE_TO)
    except Exception as e:  # noqa: BLE001
        print("Could not send failure note:", e, file=sys.stderr)


def fetch_invoice_items(invoice_ids: list[str]) -> dict[str, list[dict]]:
    by_inv: dict[str, list[dict]] = {i: [] for i in invoice_ids}
    if not invoice_ids:
        return by_inv
    # Fetch recent invoice items and group; Shepherd often lacks reliable invoiceId filter.
    # Prefer per-invoice fetch when possible.
    for inv_id in invoice_ids:
        try:
            items = shepherd_paginated(
                "open-api-invoice-items",
                {"invoiceId": inv_id, "isDeleted": False, "embed": "product"},
                rpp=200,
            )
            # Some APIs ignore invoiceId — filter client-side
            filtered = [
                it
                for it in items
                if str(it.get("invoiceId") or (it.get("invoice") or {}).get("id") or "") == inv_id
            ]
            by_inv[inv_id] = filtered if filtered else [
                it for it in items if money(it.get("total") or it.get("amount")) >= 0
            ][:0]  # if filter failed empty, leave empty rather than wrong items
            if not filtered and items:
                # Heuristic: if response is tiny and all share same invoiceId missing, use as-is only when len small
                same = all(
                    str(it.get("invoiceId") or "") in ("", inv_id) for it in items[:20]
                )
                if len(items) <= 10 and same:
                    by_inv[inv_id] = items
        except Exception as e:  # noqa: BLE001
            print(f"invoice items fetch failed for {inv_id}: {e}", file=sys.stderr)
    return by_inv


def credit_balances(client_ids: list[str]) -> dict[str, float]:
    """Latest non-voided payment's clientBalanceAfterPayment per client."""
    credits = {cid: 0.0 for cid in client_ids}
    if not client_ids:
        return credits
    payments = shepherd_paginated(
        "open-api-payments",
        {
            "dateFrom": (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            ),
            "embed": "paymentMethod",
        },
        rpp=1000,
    )
    best: dict[str, tuple[datetime, float]] = {}
    for p in payments:
        if p.get("isDeleted") or p.get("isVoided") or p.get("voided"):
            continue
        cid = str(p.get("clientId") or "")
        if cid not in credits:
            continue
        pdt = parse_dt(p.get("paymentDate") or p.get("dateCreated"))
        if not pdt:
            continue
        bal = money(p.get("clientBalanceAfterPayment"))
        prev = best.get(cid)
        if prev is None or pdt > prev[0]:
            best[cid] = (pdt, bal)
    for cid, (_, bal) in best.items():
        credits[cid] = bal
    return credits


def qualify(now: datetime) -> tuple[list[dict], list[dict], dict]:
    """Return (qualified_to_email, paid_since_rows, debug)."""
    date_from = (now - timedelta(days=1)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    appts = shepherd_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "embed": "client,appointmentStatus,appointmentType",
            "isDeleted": False,
        },
        rpp=500,
    )
    upcoming = [a for a in appts if is_upcoming(a, now)]
    # Only consider those within 14 days for reminders
    windowed = []
    for a in upcoming:
        start = parse_dt(a.get("startDate"))
        if start and current_milestone(start, now):
            windowed.append(a)

    invoices = shepherd_paginated(
        "open-api-invoices",
        {
            "isDeleted": False,
            "embed": "invoiceStatus,invoiceItems,client",
        },
        rpp=500,
    )
    # Keep Active open balances
    active_open = [
        inv
        for inv in invoices
        if not inv.get("isDeleted")
        and invoice_status_name(inv).lower() == "active"
        and money(inv.get("balance")) > 0
    ]

    # Map clientId -> list of active open invoices
    by_client_inv: dict[str, list[dict]] = {}
    for inv in active_open:
        cid = str(inv.get("clientId") or "")
        if cid:
            by_client_inv.setdefault(cid, []).append(inv)

    # Gather invoice items for deposit-style check
    need_items = []
    for invs in by_client_inv.values():
        for inv in invs:
            embedded = inv.get("invoiceItems") or []
            if not embedded:
                need_items.append(str(inv["id"]))
    fetched_items = fetch_invoice_items(need_items)

    def items_for(inv: dict) -> list[dict]:
        emb = inv.get("invoiceItems")
        if isinstance(emb, list) and emb:
            return [i for i in emb if not i.get("isDeleted")]
        return fetched_items.get(str(inv["id"]), [])

    client_ids = list(
        {
            str(a.get("clientId") or (a.get("client") or {}).get("id") or "")
            for a in windowed
            if a.get("clientId") or (a.get("client") or {}).get("id")
        }
        | {str(t.get("client_id") or "") for t in TRACKED_UNPAID if t.get("client_id")}
    )
    client_ids = [c for c in client_ids if c]
    credits = credit_balances(client_ids)

    # Also need credits for clients with active deposits in window
    more_ids = [cid for cid in by_client_inv if cid not in credits]
    if more_ids:
        credits.update(credit_balances(more_ids))

    # Build qualified candidates
    candidates: list[dict] = []
    exclusions: list[str] = []

    for appt in windowed:
        cid = str(appt.get("clientId") or (appt.get("client") or {}).get("id") or "")
        client = appt.get("client") or {}
        name = (
            client.get("name")
            or " ".join(
                filter(
                    None,
                    [client.get("firstName"), client.get("lastName")],
                )
            )
            or "Unknown"
        ).strip()
        email = (client.get("email") or "").strip()
        start = parse_dt(appt.get("startDate"))
        if not start or not cid:
            exclusions.append(f"{name}: missing client/start")
            continue
        milestone = current_milestone(start, now)
        if not milestone:
            continue
        aid = str(appt.get("id"))
        if (aid, milestone) in SENT_MILESTONES:
            exclusions.append(f"{name}: {milestone} already sent for appt {aid}")
            continue
        if not usable_email(email):
            exclusions.append(f"{name}: no usable email")
            continue
        credit = money(credits.get(cid, 0.0))
        if credit != 0.0:
            exclusions.append(f"{name}: credit {fmt_money(credit)} ≠ $0.00")
            continue
        invs = by_client_inv.get(cid, [])
        deposit_invs = []
        for inv in invs:
            its = items_for(inv)
            if is_deposit_style(inv, its):
                deposit_invs.append((inv, its))
        if not deposit_invs:
            exclusions.append(f"{name}: no Active outstanding deposit-style invoice")
            continue
        # Prefer oldest open deposit invoice
        deposit_invs.sort(
            key=lambda pair: parse_dt(pair[0].get("dateCreated") or pair[0].get("dateIssued"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        inv, its = deposit_invs[0]
        if not invoice_age_ok(inv, now):
            created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
            exclusions.append(
                f"{name}: invoice #{inv.get('invoiceNumber') or inv.get('number')} age ≤48h ({created})"
            )
            continue
        candidates.append(
            {
                "appointment_id": aid,
                "client_id": cid,
                "client": name,
                "first_name": first_name(
                    client.get("firstName") or name.split()[0] if name else "there"
                ),
                "email": email,
                "start": start,
                "appointment": format_appt_short(start),
                "appointment_phrase": format_appt_phrase(start),
                "milestone": milestone,
                "outstanding": money(inv.get("balance")),
                "credit": credit,
                "invoice_id": str(inv.get("id")),
                "invoice": f"#{inv.get('invoiceNumber') or inv.get('number') or inv.get('id')}",
                "invoice_number": str(inv.get("invoiceNumber") or inv.get("number") or ""),
            }
        )

    # Dedupe by invoice: one email per open deposit invoice (earliest appointment)
    by_inv: dict[str, list[dict]] = {}
    for c in candidates:
        by_inv.setdefault(c["invoice_id"], []).append(c)
    qualified: list[dict] = []
    sibling_marks: list[dict] = []
    for inv_id, group in by_inv.items():
        group.sort(key=lambda x: x["start"])
        primary = group[0]
        primary["sibling_appointment_ids"] = [g["appointment_id"] for g in group[1:]]
        qualified.append(primary)
        for g in group[1:]:
            sibling_marks.append(g)

    # Paid-since: TRACKED_UNPAID currently empty — still scan known prior LIVE-sent
    # clients who were on recent reports. We only report those in TRACKED_UNPAID.
    paid_rows: list[dict] = []
    for t in TRACKED_UNPAID:
        cid = str(t.get("client_id") or "")
        prior_inv = str(t.get("invoice_number") or "")
        credit = money(credits.get(cid, 0.0)) if cid else 0.0
        still_open = False
        status_now = None
        evidence = None
        for inv in active_open:
            if str(inv.get("clientId")) != cid:
                continue
            num = str(inv.get("invoiceNumber") or inv.get("number") or "")
            if prior_inv and num == prior_inv and money(inv.get("balance")) > 0:
                still_open = True
                break
        if still_open and credit == 0.0:
            continue  # still qualifies unpaid
        if credit > 0:
            status_now = "Paid / credit on account"
            evidence = f"Credit now {fmt_money(credit)}; prior {t.get('invoice')} still/was open"
        elif not still_open:
            # Check if invoice exists but not Active/open
            matching = [
                inv
                for inv in invoices
                if str(inv.get("clientId")) == cid
                and str(inv.get("invoiceNumber") or inv.get("number") or "") == prior_inv
            ]
            if matching:
                inv = matching[0]
                st = invoice_status_name(inv)
                bal = money(inv.get("balance"))
                if bal <= 0:
                    status_now = "Balance cleared"
                    evidence = f"Invoice {t.get('invoice')} status {st}; balance {fmt_money(bal)}"
                else:
                    status_now = "No longer Active open deposit"
                    evidence = f"Invoice {t.get('invoice')} status {st}; balance {fmt_money(bal)}"
            else:
                status_now = "No longer Active open deposit"
                evidence = f"Prior {t.get('invoice')} not found as Active open"
        else:
            continue
        paid_rows.append(
            {
                "client": t.get("client") or "Unknown",
                "appointment": t.get("appointment") or "",
                "prior_milestone": t.get("milestone") or "",
                "prior_outstanding": t.get("outstanding") or "",
                "status_now": status_now,
                "evidence": evidence or "",
            }
        )

    debug = {
        "upcoming_count": len(upcoming),
        "windowed_count": len(windowed),
        "active_open_count": len(active_open),
        "candidates_before_dedupe": len(candidates),
        "qualified": len(qualified),
        "exclusions": exclusions,
        "siblings": [
            {"client": s["client"], "appointment_id": s["appointment_id"], "invoice": s["invoice"]}
            for s in sibling_marks
        ],
    }
    return qualified, paid_rows, debug


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="LIVE: send client emails + staff report")
    parser.add_argument("--qualify-only", action="store_true", help="Qualify only; no Gmail sends")
    args = parser.parse_args()
    if not args.send and not args.qualify_only:
        args.send = True  # default LIVE for this automation

    now = datetime.now(timezone.utc)
    report_date = now.astimezone(TZ).strftime("%B %-d, %Y").replace(" 0", " ")
    # %-d may not work on all platforms
    report_date = now.astimezone(TZ).strftime("%B %d, %Y").replace(" 0", " ")

    print(f"Run at {now.isoformat()} | report_date={report_date} | mode={'LIVE' if args.send else 'qualify-only'}")

    try:
        qualified, paid_rows, debug = qualify(now)
    except Exception as e:  # noqa: BLE001
        print("Shepherd qualification failed:", e, file=sys.stderr)
        if args.send:
            try_failure_note(f"Shepherd qualification failed during deposit reminder run: {e}")
        return 1

    print(json.dumps({"debug_summary": {k: debug[k] for k in debug if k != "exclusions"}, "exclusions": debug["exclusions"], "qualified": [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in q.items() if k != "sibling_appointment_ids"}
        | {"siblings": q.get("sibling_appointment_ids", [])}
        for q in qualified
    ], "paid_since": paid_rows}, indent=2, default=str))

    result = {
        "mode": "LIVE" if args.send else "qualify-only",
        "report_date": report_date,
        "sent": [],
        "sibling_marks": [],
        "staff_report_id": None,
        "signature_ok": False,
        "paid_since": paid_rows,
        "exclusions": debug["exclusions"],
        "error": None,
    }

    if args.qualify_only:
        print(json.dumps(result, indent=2, default=str))
        return 0

    # LIVE sends
    try:
        c = get_composio()
        signature_html = load_signature(c)
        result["signature_ok"] = True
        result["signature_len"] = len(signature_html)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"Gmail signature load failed: {e}"
        print(result["error"], file=sys.stderr)
        try_failure_note(result["error"])
        # Still try to send staff report? Template says fail Gmail step if signature cannot load.
        return 2

    sent_rows = []
    for q in qualified:
        body = reminder_html(q["first_name"], q["appointment_phrase"], q["outstanding"])
        html = body + "<br>" + signature_html
        try:
            mid = gmail_send(c, q["email"], CLIENT_SUBJECT, html)
            entry = {
                "client": q["client"],
                "email": q["email"],
                "appointment": q["appointment"],
                "milestone": q["milestone"],
                "outstanding": fmt_money(q["outstanding"]),
                "credit": fmt_money(q["credit"]),
                "invoice": q["invoice"],
                "appointment_id": q["appointment_id"],
                "client_id": q["client_id"],
                "gmail_id": mid,
                "siblings": q.get("sibling_appointment_ids", []),
            }
            sent_rows.append(entry)
            result["sent"].append(entry)
            print(f"SENT {q['milestone']} → {q['client']} <{q['email']}> {q['appointment']} {q['invoice']} id={mid}")
        except Exception as e:  # noqa: BLE001
            print(f"Failed sending to {q['client']}: {e}", file=sys.stderr)
            result["error"] = f"Client send failed for {q['client']}: {e}"
            try_failure_note(result["error"])

    # Staff report
    report_body = staff_report_html(report_date, sent_rows, paid_rows) + "<br>" + signature_html
    staff_subject = f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}"
    try:
        rid = gmail_send(c, STAFF_TO, staff_subject, report_body)
        result["staff_report_id"] = rid
        print(f"Staff report SENT id={rid} subject={staff_subject}")
    except Exception as e:  # noqa: BLE001
        result["error"] = f"Staff report send failed: {e}"
        print(result["error"], file=sys.stderr)
        try_failure_note(result["error"])
        return 3

    out_path = "/tmp/deposit_reminder_result.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print("Wrote", out_path)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
