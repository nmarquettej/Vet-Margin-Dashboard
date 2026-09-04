#!/usr/bin/env python3
"""Whisker Waggin' appointment deposit reminder — LIVE mode runner.

Qualification + optional LIVE send of client reminders and staff report.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("America/New_York")
CANCEL_STATUSES = {"canceled", "cancelled", "checked out", "no show"}
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
NOREPLY = "noreply@whiskerwagginwnc.com"
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAIL_TO = "nmarquettej@gmail.com"
PORTAL = "https://petportal.vet/whisker-waggin-wnc/payment/0"
GMAIL_VERSION = "20260721_00"

# Already-sent milestones from memory (appointment_id|milestone) LIVE-sent only
ALREADY_SENT = {
    "1555f827-2570-419e-a21d-b48e01519e2f|1-week",
    "eed3a028-dd71-4817-a713-b49100dae383|1-week",
    "72bec82f-7cd7-4731-8695-b491018afaa0|2-week",
    "1d7a4d29-bcde-451c-acb3-b49501776b95|1-week",
    "2792a999-5908-4220-bce8-b48301070f12|1-week",
    "9377b397-34cd-4739-af0b-b495017a430e|1-week",
    "9377b397-34cd-4739-af0b-b495017a430e|48-hour",
    "8174970a-2a7b-4525-837d-b4a200e5ceb6|1-week",
    "3eefb3be-5c2c-4284-9f61-b48d00dba095|2-week",
    "c5157f6f-d9b9-434a-af39-b4a500dede04|2-week",
    "345a3404-6cbe-470b-bfad-b4a601681146|2-week",
    "66a63a00-cead-42dd-ac2b-b4a900db134d|2-week",
    "e88a5dd0-4c20-4887-b631-b4b2010a52f6|48-hour",
}

# Prior tracked unpaid for paid-since (empty as of 2026-09-03)
PRIOR_UNPAID: list[dict[str, Any]] = []


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        raise RuntimeError(f"Missing env {name}")
    return v


def shepherd_headers() -> dict[str, str]:
    return {
        "X-Integration-Public-Key": env("SHEPHERD_PUBLIC_KEY"),
        "X-Integration-Private-Key": env("SHEPHERD_PRIVATE_KEY"),
        "X-Clinic-Id": env("SHEPHERD_CLINIC_ID"),
        "Content-Type": "application/json",
    }


def shepherd_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    base = env("SHEPHERD_BASE_URL").rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, headers=shepherd_headers(), json=body)
        r.raise_for_status()
        return r.json()


def paginate(path: str, body: dict[str, Any], rpp: int = 200) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {**body, "page": page, "rpp": rpp}
        data = shepherd_post(path, payload)
        batch = data.get("item") or data.get("items") or []
        if not isinstance(batch, list):
            batch = []
        items.extend(batch)
        total = data.get("totalRecords")
        if total is not None and len(items) >= int(total):
            break
        if len(batch) < rpp:
            break
        page += 1
        if page > 50:
            break
    return items


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def money(n: Any) -> float:
    try:
        return round(float(n or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def fmt_money(n: float) -> str:
    return f"{n:,.2f}"


def usable_email(email: Any) -> str | None:
    if not email or not isinstance(email, str):
        return None
    e = email.strip()
    if not e or "@" not in e:
        return None
    if e.lower() in {"none", "null", "n/a", "na"}:
        return None
    return e


def client_name(c: dict[str, Any] | None) -> tuple[str, str]:
    if not c:
        return ("", "Unknown")
    first = (c.get("firstName") or c.get("firstname") or "").strip()
    last = (c.get("lastName") or c.get("lastname") or "").strip()
    full = (f"{first} {last}").strip() or (c.get("name") or "Unknown").strip()
    return first or full.split()[0], full


def status_name(appt: dict[str, Any]) -> str:
    st = appt.get("appointmentStatus") or {}
    if isinstance(st, dict):
        return (st.get("name") or "").strip()
    return str(st or "").strip()


def milestone_for(start: datetime, now: datetime) -> str | None:
    delta = start - now
    if delta.total_seconds() <= 0:
        return None
    hours = delta.total_seconds() / 3600.0
    days = delta.total_seconds() / 86400.0
    if hours <= 48:
        return "48-hour"
    if days <= 7:
        return "1-week"
    if days <= 14:
        return "2-week"
    return None


def fmt_appt(start: datetime) -> str:
    local = start.astimezone(TZ)
    day = local.strftime("%A")
    md = f"{local.month}/{local.day}"
    t = local.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day}, {md} at {t}"


def line_name(it: dict[str, Any]) -> str:
    name = (it.get("name") or it.get("description") or it.get("productName") or "").strip()
    if not name and isinstance(it.get("product"), dict):
        name = (it["product"].get("name") or "").strip()
    return name


def is_deposit_style(invoice: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """Prefer correct non-send: exam/wellness+travel or travel-only ≤$150."""
    lines = []
    for it in items:
        if it.get("isDeleted") or it.get("deleted"):
            continue
        name = line_name(it)
        amt = money(it.get("total") or it.get("amount") or it.get("price") or 0)
        if not name and amt == 0:
            continue
        lines.append((name.lower(), name, amt))
    if not lines:
        # Prefer correct non-send when line detail unavailable
        return False

    if len(lines) > 4:
        return False

    names = [n for n, _, _ in lines]
    joined = " | ".join(names)

    # Exclude obvious non-deposit AR / product-only invoices
    exclude_kw = [
        "revolution", "cerenia", "douxo", "vaccine", "vaccination", "felv", "fvrcp",
        "rabies", "blood", "chem", "dental", "surgery", "euthan", "cremat", "neuter",
        "spay", "ultrasound", "x-ray", "radiograph", "anesthes", "wipe", "injection",
    ]
    # Product-only AR (no exam/travel) or meds mixed in — prefer non-send for product keywords
    # unless the only lines are exam+travel (keywords shouldn't appear there).
    if any(k in joined for k in exclude_kw):
        return False

    has_exam = any(
        ("exam" in n) or ("wellness" in n) or ("office visit" in n) or ("recheck" in n)
        for n in names
    )
    has_travel = any("travel" in n for n in names)
    only_travel = len(lines) == 1 and has_travel and lines[0][2] <= 150

    if only_travel:
        return True
    if has_exam and has_travel and len(lines) <= 4:
        # Ensure no large extra product lines beyond exam/travel (+ maybe small misc)
        other = [
            amt
            for n, _, amt in lines
            if "exam" not in n
            and "wellness" not in n
            and "travel" not in n
            and "recheck" not in n
            and "office visit" not in n
        ]
        if other and sum(other) > 50:
            return False
        return True
    return False


def invoice_age_ok(inv: dict[str, Any], now: datetime) -> bool:
    created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
    if not created:
        return False  # prefer non-send
    return (now - created) > timedelta(hours=48)


def build_reminder_html(first: str, appt_str: str, balance: float, signature_html: str) -> str:
    body = f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #222; line-height: 1.5;">
<p>Hey {html.escape(first)},</p>
<p>This is a friendly reminder of your upcoming appointment with Whisker Waggin' Mobile Veterinary Care on {html.escape(appt_str)}.</p>
<p>Our system indicates you have not yet paid the outstanding balance on your account in the amount of ${html.escape(fmt_money(balance))} for the appointment deposit. For your convenience and to keep your visit running smoothly, we kindly ask that all appointment deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to your visit, we will unfortunately need to cancel the trip.</p>
<p>You can view and pay your open invoice through the Pet Portal linked below. Our team can also take payment directly over the phone at the number below. If you have any questions or need assistance, please reach out to <a href="mailto:admin@whiskerwagginwnc.com">admin@whiskerwagginwnc.com</a> and our team would be happy to help.</p>
<p><a href="{PORTAL}">{PORTAL}</a></p>
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
    def table(headers: list[str], rows: list[list[str]], empty: str) -> str:
        th = "".join(f'<th style="border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;">{html.escape(h)}</th>' for h in headers)
        if not rows:
            return f'<p><em>{html.escape(empty)}</em></p>'
        trs = []
        for row in rows:
            tds = "".join(f'<td style="border:1px solid #ccc;padding:6px 10px;">{html.escape(c)}</td>' for c in row)
            trs.append(f"<tr>{tds}</tr>")
        return f'<table style="border-collapse:collapse;font-size:13px;margin:8px 0;"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'

    sent_data = [
        [
            r["client"],
            r["email"],
            r["appointment"],
            r["milestone"],
            f"${fmt_money(r['outstanding'])}",
            f"${fmt_money(r['credit'])}",
            r["invoice"],
        ]
        for r in sent_rows
    ]
    paid_data = [
        [
            r["client"],
            r["appointment"],
            r["prior_milestone"],
            f"${fmt_money(r['prior_outstanding'])}",
            r["status_now"],
            r["evidence"],
        ]
        for r in paid_rows
    ]

    content = f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #222;">
<h2 style="margin:0 0 12px;">Outstanding Appointment Deposit Report — {html.escape(report_date)}</h2>
<p>Run mode: <strong>LIVE</strong> (client reminders sent when qualified).</p>
<h3 style="margin:18px 0 6px;">Section 1 — Emails sent this run</h3>
{table(["Client", "Email", "Appointment", "Milestone", "Outstanding", "Credit", "Invoice"], sent_data, "No qualifying clients this run.")}
<h3 style="margin:18px 0 6px;">Section 2 — Paid since last report</h3>
{table(["Client", "Appointment", "Prior milestone", "Prior outstanding", "Status now", "Evidence"], paid_data, "No previously reported clients have paid since the last run.")}
</div>
<br>
{signature_html}"""
    return content


def get_composio():
    from composio import Composio

    return Composio(
        api_key=env("COMPOSIO_API_KEY"),
        toolkit_versions={"gmail": GMAIL_VERSION},
    )


def gmail_execute(c, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return c.tools.execute(
        slug=slug,
        user_id=NOREPLY,
        version=GMAIL_VERSION,
        arguments=arguments,
    )


def load_signature(c) -> str:
    r = gmail_execute(c, "GMAIL_LIST_SEND_AS", {})
    data = r.get("data") or {}
    send_as = data.get("sendAs") or []
    if not send_as:
        raise RuntimeError("GMAIL_LIST_SEND_AS returned no sendAs entries")
    # Prefer primary / noreply
    chosen = None
    for sa in send_as:
        if sa.get("isPrimary") or sa.get("isDefault"):
            chosen = sa
            break
        if (sa.get("sendAsEmail") or "").lower() == NOREPLY.lower():
            chosen = sa
    if not chosen:
        chosen = send_as[0]
    sig = (chosen.get("signature") or "").strip()
    if not sig:
        raise RuntimeError("NoReply send-as signature empty/missing")
    return sig


def send_email(c, to: str, subject: str, body_html: str) -> str:
    r = gmail_execute(
        c,
        "GMAIL_SEND_EMAIL",
        {
            "recipient_email": to,
            "subject": subject,
            "body": body_html,
            "is_html": True,
        },
    )
    data = r.get("data") or r
    mid = (
        data.get("id")
        or data.get("messageId")
        or data.get("response_data", {}).get("id")
        or ""
    )
    if isinstance(mid, dict):
        mid = mid.get("id") or ""
    # nested success patterns
    if not mid and isinstance(data, dict):
        for k in ("message", "response", "data"):
            sub = data.get(k)
            if isinstance(sub, dict) and sub.get("id"):
                mid = sub["id"]
                break
    if not mid:
        # still treat as success if successfultrue
        if r.get("successful") is False or r.get("error"):
            raise RuntimeError(f"Gmail send failed: {r}")
        mid = json.dumps(data)[:80]
    return str(mid)


def fetch_invoice_items(invoice_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Paginate all invoice items and keep those matching target invoice ids.

    Shepherd's invoiceId/ids filters on open-api-invoice-items are unreliable;
    client-side filter on invoiceId is required.
    """
    wanted = set(str(i) for i in invoice_ids)
    by_inv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not wanted:
        return by_inv
    items = paginate("open-api-invoice-items", {}, rpp=500)
    for it in items:
        iid = it.get("invoiceId") or (it.get("invoice") or {}).get("id")
        if iid and str(iid) in wanted:
            by_inv[str(iid)].append(it)
    return by_inv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--send", action="store_true", help="LIVE: send client + staff emails")
    parser.add_argument("--out", default="/tmp/deposit_reminder_result.json")
    args = parser.parse_args()
    if not args.send and not args.qualify_only:
        args.qualify_only = True

    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "now_utc": now.isoformat(),
        "mode": "LIVE-send" if args.send else "qualify-only",
        "qualified": [],
        "sent": [],
        "paid_since": [],
        "exclusions": [],
        "errors": [],
        "staff_report": None,
    }

    try:
        # Appointments from ~now looking ahead 30 days (filter client-side to 14d for milestones)
        date_from = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        appts = paginate(
            "open-api-appointments",
            {
                "dateFrom": date_from,
                "embed": "client,appointmentStatus,appointmentType",
            },
        )
        upcoming = []
        for a in appts:
            if a.get("isDeleted"):
                continue
            start = parse_dt(a.get("startDate"))
            if not start or start <= now:
                continue
            st = status_name(a).lower()
            if st in CANCEL_STATUSES:
                continue
            if not a.get("clientId") and not (a.get("client") or {}).get("id"):
                continue
            upcoming.append(a)

        # Active invoices
        invoices = paginate(
            "open-api-invoices",
            {"embed": "invoiceStatus,client"},
        )
        active_open = []
        for inv in invoices:
            if inv.get("isDeleted"):
                continue
            st = ((inv.get("invoiceStatus") or {}).get("name") or "").strip()
            if st != "Active":
                continue
            if money(inv.get("balance")) <= 0:
                continue
            active_open.append(inv)

        # Payments for credit proxy
        payments = paginate("open-api-payments", {})
        latest_credit: dict[str, tuple[datetime, float]] = {}
        for p in payments:
            if p.get("isDeleted") or p.get("isVoided") or p.get("voided"):
                continue
            cid = str(p.get("clientId") or "")
            if not cid:
                continue
            pdt = parse_dt(p.get("paymentDate") or p.get("dateCreated"))
            if not pdt:
                continue
            bal = money(p.get("clientBalanceAfterPayment"))
            prev = latest_credit.get(cid)
            if not prev or pdt > prev[0]:
                latest_credit[cid] = (pdt, bal)

        # Invoice items for deposit heuristic
        inv_ids = [str(i["id"]) for i in active_open if i.get("id")]
        items_by_inv = fetch_invoice_items(inv_ids)

        # Map client -> deposit-style active open invoices
        deposit_by_client: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for inv in active_open:
            iid = str(inv["id"])
            items = items_by_inv.get(iid, [])
            # Also try embedded invoiceItems
            if not items and inv.get("invoiceItems"):
                items = inv["invoiceItems"]
            if not is_deposit_style(inv, items):
                continue
            cid = str(inv.get("clientId") or (inv.get("client") or {}).get("id") or "")
            if cid:
                deposit_by_client[cid].append(inv)

        # Sent milestones set (mutable)
        sent_milestones = set(ALREADY_SENT)

        # Group upcoming by client
        by_client: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in upcoming:
            cid = str(a.get("clientId") or (a.get("client") or {}).get("id") or "")
            if cid:
                by_client[cid].append(a)

        # Candidates within 14 days
        candidates = []
        for cid, appt_list in by_client.items():
            appt_list.sort(key=lambda x: parse_dt(x["startDate"]) or now)
            for a in appt_list:
                start = parse_dt(a["startDate"])
                if not start:
                    continue
                ms = milestone_for(start, now)
                if not ms:
                    continue
                candidates.append((cid, a, start, ms))

        # Dedupe: one email per open deposit invoice — earliest appt
        # First build invoice association per client (pick primary deposit inv)
        qualified: list[dict[str, Any]] = []
        used_invoices: set[str] = set()
        sibling_marks: list[tuple[str, str]] = []  # (appt_id, milestone) for siblings

        # Process earliest appointments first
        candidates.sort(key=lambda t: t[2])

        for cid, appt, start, ms in candidates:
            client = appt.get("client") or {}
            first, full = client_name(client if isinstance(client, dict) else None)
            email = usable_email(client.get("email") if isinstance(client, dict) else None)

            deposits = deposit_by_client.get(cid, [])
            if not deposits:
                result["exclusions"].append(
                    {
                        "client": full or cid,
                        "reason": "no Active outstanding deposit-style invoice",
                        "appointment": fmt_appt(start),
                    }
                )
                continue

            # Prefer invoice matching appointmentId if any, else oldest open deposit
            inv = None
            for d in deposits:
                if str(d.get("appointmentId") or "") == str(appt.get("id")):
                    inv = d
                    break
            if not inv:
                inv = sorted(
                    deposits,
                    key=lambda d: parse_dt(d.get("dateCreated")) or parse_dt(d.get("dateIssued")) or now,
                )[0]

            iid = str(inv["id"])
            if iid in used_invoices:
                # sibling slot — mark as sent if we qualify sibling later; skip email
                key = f"{appt['id']}|{ms}"
                sibling_marks.append((str(appt["id"]), ms))
                result["exclusions"].append(
                    {
                        "client": full,
                        "reason": f"sibling-dedupe same invoice #{inv.get('invoiceNumber') or iid}",
                        "appointment": fmt_appt(start),
                    }
                )
                continue

            credit = latest_credit.get(cid, (None, 0.0))[1]
            if credit != 0.0:
                result["exclusions"].append(
                    {
                        "client": full,
                        "reason": f"credit balance ${fmt_money(credit)} ≠ $0.00",
                        "appointment": fmt_appt(start),
                        "invoice": f"#{inv.get('invoiceNumber')} ${fmt_money(money(inv.get('balance')))}",
                    }
                )
                continue

            if not email:
                result["exclusions"].append(
                    {
                        "client": full,
                        "reason": "no usable email",
                        "appointment": fmt_appt(start),
                    }
                )
                continue

            key = f"{appt['id']}|{ms}"
            if key in sent_milestones:
                result["exclusions"].append(
                    {
                        "client": full,
                        "reason": f"milestone {ms} already sent",
                        "appointment": fmt_appt(start),
                    }
                )
                continue

            if not invoice_age_ok(inv, now):
                created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
                age_h = (now - created).total_seconds() / 3600.0 if created else None
                result["exclusions"].append(
                    {
                        "client": full,
                        "reason": f"invoice age ≤48h ({age_h:.1f}h)" if age_h is not None else "invoice age unknown",
                        "appointment": fmt_appt(start),
                        "invoice": f"#{inv.get('invoiceNumber')} created {created.isoformat() if created else '?'}",
                    }
                )
                continue

            bal = money(inv.get("balance"))
            row = {
                "appointment_id": str(appt["id"]),
                "client_id": cid,
                "client": full,
                "first_name": first,
                "email": email,
                "appointment": fmt_appt(start),
                "appointment_start_utc": start.isoformat(),
                "milestone": ms,
                "outstanding": bal,
                "credit": credit,
                "invoice": f"#{inv.get('invoiceNumber')}",
                "invoice_id": iid,
                "invoice_number": inv.get("invoiceNumber"),
                "invoice_created": (parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))).isoformat(),
            }
            qualified.append(row)
            used_invoices.add(iid)

            # Mark sibling appointments same day/client with same invoice for milestone tracking after send
            for other in by_client.get(cid, []):
                oid = str(other.get("id"))
                if oid == str(appt["id"]):
                    continue
                ostart = parse_dt(other.get("startDate"))
                if not ostart:
                    continue
                oms = milestone_for(ostart, now)
                if oms == ms:
                    # same invoice association (no appointmentId link) — treat as sibling
                    row.setdefault("sibling_appointment_ids", []).append(oid)

        result["qualified"] = qualified

        # Paid-since: PRIOR_UNPAID empty
        paid_since: list[dict[str, Any]] = []
        for prior in PRIOR_UNPAID:
            pcid = prior.get("client_id")
            credit = latest_credit.get(str(pcid), (None, 0.0))[1] if pcid else 0.0
            still_open = [
                d
                for d in deposit_by_client.get(str(pcid), [])
                if str(d.get("id")) == str(prior.get("invoice_id"))
                or str(d.get("invoiceNumber")) == str(prior.get("invoice_number"))
            ]
            status_now = None
            evidence = ""
            if credit > 0:
                status_now = "Paid / credit on account"
                evidence = f"credit now ${fmt_money(credit)}"
            elif not still_open:
                # Check if invoice still Active with balance
                match = [
                    i
                    for i in invoices
                    if str(i.get("id")) == str(prior.get("invoice_id"))
                    or str(i.get("invoiceNumber")) == str(prior.get("invoice_number"))
                ]
                if match:
                    st = ((match[0].get("invoiceStatus") or {}).get("name") or "")
                    bal = money(match[0].get("balance"))
                    if bal <= 0:
                        status_now = "Balance cleared"
                        evidence = f"invoice #{prior.get('invoice_number')} balance ${fmt_money(bal)} ({st})"
                    elif st != "Active":
                        status_now = "No longer Active open deposit"
                        evidence = f"invoice #{prior.get('invoice_number')} status {st}"
                    else:
                        # still active open but not deposit-style? treat as cleared deposit heuristic
                        status_now = "No longer Active open deposit"
                        evidence = f"invoice #{prior.get('invoice_number')} no longer deposit-style open"
                else:
                    status_now = "No longer Active open deposit"
                    evidence = f"invoice #{prior.get('invoice_number')} not found / closed"
            if status_now:
                paid_since.append(
                    {
                        "client": prior.get("client"),
                        "appointment": prior.get("appointment"),
                        "prior_milestone": prior.get("milestone"),
                        "prior_outstanding": prior.get("outstanding"),
                        "status_now": status_now,
                        "evidence": evidence,
                        "client_id": pcid,
                        "invoice_id": prior.get("invoice_id"),
                    }
                )
        result["paid_since"] = paid_since

        if args.qualify_only and not args.send:
            with open(args.out, "w") as f:
                json.dump(result, f, indent=2)
            print(json.dumps({"qualified_count": len(qualified), "exclusions": len(result["exclusions"]), "out": args.out}))
            return 0

        # LIVE send
        c = get_composio()
        try:
            signature_html = load_signature(c)
            result["signature_chars"] = len(signature_html)
        except Exception as e:
            result["errors"].append(f"Signature load failed: {e}")
            try:
                send_email(
                    c,
                    FAIL_TO,
                    "[WWMVC] Deposit reminder failure — signature",
                    f"<p>Deposit reminder run failed loading Gmail signature: {html.escape(str(e))}</p>",
                )
            except Exception as e2:
                result["errors"].append(f"Failure note also failed: {e2}")
            with open(args.out, "w") as f:
                json.dump(result, f, indent=2)
            return 1

        sent_rows = []
        for row in qualified:
            body = build_reminder_html(
                row["first_name"], row["appointment"], row["outstanding"], signature_html
            )
            try:
                mid = send_email(c, row["email"], CLIENT_SUBJECT, body)
                sent_entry = {**row, "gmail_id": mid, "mode": "LIVE-sent"}
                sent_rows.append(sent_entry)
                result["sent"].append(sent_entry)
                sent_milestones.add(f"{row['appointment_id']}|{row['milestone']}")
                for sib in row.get("sibling_appointment_ids") or []:
                    sent_milestones.add(f"{sib}|{row['milestone']}")
                    result["sent"].append(
                        {
                            **row,
                            "appointment_id": sib,
                            "gmail_id": mid,
                            "mode": "LIVE-sent(sibling-dedupe)",
                        }
                    )
            except Exception as e:
                result["errors"].append(f"Send failed for {row['client']}: {e}")

        # Staff report
        report_date = now.astimezone(TZ).strftime("%B %-d, %Y").replace(" 0", " ")
        # Python %-d may not work on all platforms
        report_date = now.astimezone(TZ).strftime("%B %d, %Y").replace(" 0", " ")
        staff_body = build_staff_report_html(report_date, sent_rows, paid_since, signature_html)
        staff_subject = f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}"
        try:
            staff_mid = send_email(c, STAFF_TO, staff_subject, staff_body)
            result["staff_report"] = {
                "to": STAFF_TO,
                "subject": staff_subject,
                "gmail_id": staff_mid,
            }
        except Exception as e:
            result["errors"].append(f"Staff report send failed: {e}")
            try:
                send_email(
                    c,
                    FAIL_TO,
                    "[WWMVC] Deposit reminder failure — staff report",
                    f"<p>Staff report failed: {html.escape(str(e))}</p>",
                )
            except Exception as e2:
                result["errors"].append(f"Failure note also failed: {e2}")

        # New tracked unpaid = sent this run (actual clients, not siblings)
        result["tracked_unpaid_next"] = [
            {
                "appointment_id": r["appointment_id"],
                "client_id": r["client_id"],
                "client": r["client"],
                "email": r["email"],
                "appointment": r["appointment"],
                "milestone": r["milestone"],
                "outstanding": r["outstanding"],
                "invoice": r["invoice"],
                "invoice_id": r["invoice_id"],
                "invoice_number": r.get("invoice_number"),
            }
            for r in sent_rows
        ]
        # Keep prior unpaid who did not pay
        for prior in PRIOR_UNPAID:
            if any(p.get("client_id") == prior.get("client_id") for p in paid_since):
                continue
            if any(t.get("client_id") == prior.get("client_id") for t in result["tracked_unpaid_next"]):
                continue
            result["tracked_unpaid_next"].append(prior)

        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(
            json.dumps(
                {
                    "qualified": len(qualified),
                    "sent": len(sent_rows),
                    "staff": result.get("staff_report"),
                    "errors": result["errors"],
                    "out": args.out,
                }
            )
        )
        return 0 if not result["errors"] else 2

    except Exception as e:
        result["errors"].append(str(e))
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        # try failure note
        try:
            c = get_composio()
            send_email(
                c,
                FAIL_TO,
                "[WWMVC] Deposit reminder failure",
                f"<p>Deposit reminder run failed: {html.escape(str(e))}</p>",
            )
        except Exception:
            pass
        print(json.dumps({"error": str(e), "out": args.out}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
