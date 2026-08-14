#!/usr/bin/env python3
"""LIVE MODE: Whisker Waggin appointment deposit reminder runner.

Qualifies clients via Shepherd, SENDs milestone reminder emails via Composio Gmail,
and SENDs the staff report. Use --qualify-only to print JSON without sending.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
USER_ID = "noreply@whiskerwagginwnc.com"
STAFF_TO = "courtney@whiskerwagginwnc.com"
FAIL_TO = "nmarquettej@gmail.com"
CLIENT_SUBJECT = "[WWMVC] Appointment Reminder & Account Balance Notice"
GMAIL_VERSION = "20260721_00"
PORTAL_URL = "https://petportal.vet/whisker-waggin-wnc/payment/0"

# Previously LIVE-sent unpaid tracking (from automation memory as of 2026-08-14).
# Format used for paid-since compare. Update after each live run via memory.
PRIOR_TRACKED_UNPAID: list[dict[str, Any]] = [
    {
        "client": "Ashleigh Modlin",
        "client_id": "4a689e67-176c-4e5f-a294-b4a300e3fc33",
        "appointment_id": "8174970a-2a7b-4525-837d-b4a200e5ceb6",
        "appointment": "Mon 8/17 3:30pm",
        "milestone": "1-week",
        "outstanding_fmt": "$225.00",
        "invoice_id": "fdf39789-de86-49e4-8dbf-b4a300e58116",
        "invoice": "#285",
    },
]
# Historical LIVE-sent milestones (appointment_id|milestone) — do not re-send.
SENT_MILESTONES: set[str] = {
    "1555f827-2570-419e-a21d-b48e01519e2f|1-week",
    "eed3a028-dd71-4817-a713-b49100dae383|2-week",
    "eed3a028-dd71-4817-a713-b49100dae383|1-week",
    "72bec82f-7cd7-4731-8695-b491018afaa0|2-week",
    "1d7a4d29-bcde-451c-acb3-b49501776b95|1-week",
    "2792a999-5908-4220-bce8-b48301070f12|1-week",
    "9377b397-34cd-4739-af0b-b495017a430e|1-week",
    "9377b397-34cd-4739-af0b-b495017a430e|48-hour",
    "8174970a-2a7b-4525-837d-b4a200e5ceb6|1-week",
}


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": env("SHEPHERD_PUBLIC_KEY"),
        "X-Integration-Private-Key": env("SHEPHERD_PRIVATE_KEY"),
        "X-Clinic-Id": env("SHEPHERD_CLINIC_ID"),
    }


def shepherd_post(endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = os.environ.get("SHEPHERD_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("Missing SHEPHERD_BASE_URL")
    with httpx.Client(timeout=90.0) as client:
        r = client.post(f"{base}/{endpoint.lstrip('/')}", headers=shepherd_headers(), json=body or {})
        r.raise_for_status()
        return r.json()


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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def money(v: Any) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def fmt_currency(v: float) -> str:
    return f"${v:,.2f}"


def fmt_appt_local(start: datetime) -> tuple[str, str, str]:
    local = start.astimezone(TZ)
    day = local.strftime("%A")
    date = f"{local.month}/{local.day}"
    hour = local.strftime("%I:%M%p").lstrip("0").lower()
    return day, date, hour


def client_name(client: dict | None) -> str:
    if not client:
        return "(unknown)"
    first = (client.get("firstName") or "").strip()
    last = (client.get("lastName") or "").strip()
    name = f"{first} {last}".strip()
    return name or (client.get("name") or "(unknown)")


def first_name(client: dict | None) -> str:
    if not client:
        return "there"
    return (client.get("firstName") or "").strip() or "there"


def usable_email(client: dict | None) -> str | None:
    if not client:
        return None
    email = (client.get("email") or "").strip()
    if not email or "@" not in email:
        return None
    # basic sanity
    if re.search(r"\s", email):
        return None
    return email


def status_name(obj: dict | None, key: str) -> str:
    if not obj:
        return ""
    nested = obj.get(key) or {}
    if isinstance(nested, dict):
        return (nested.get("name") or "").strip()
    return ""


def is_upcoming_appt(appt: dict, now: datetime) -> bool:
    if appt.get("isDeleted"):
        return False
    start = parse_dt(appt.get("startDate"))
    if not start or start <= now:
        return False
    st = status_name(appt, "appointmentStatus").lower()
    blocked = {"canceled", "cancelled", "checked out", "no show"}
    return st not in blocked


def milestone_for(start: datetime, now: datetime) -> str | None:
    delta = start - now
    if delta <= timedelta(0):
        return None
    if delta <= timedelta(hours=48):
        return "48-hour"
    if delta <= timedelta(days=7):
        return "1-week"
    if delta <= timedelta(days=14):
        return "2-week"
    return None


def is_deposit_style(inv: dict) -> bool:
    """Prefer non-send for large multi-service AR. Deposit-style: few lines, modest balance."""
    bal = money(inv.get("balance"))
    if bal <= 0:
        return False
    items = inv.get("invoiceItems") or inv.get("items") or []
    n = len(items) if isinstance(items, list) else 0
    # Known pattern: Wellness Exam + Travel, or travel-only small deposits.
    # Large multi-service AR (FUR-style) often 10+ lines and high balance.
    if bal >= 800 and n >= 8:
        return False
    if bal >= 1500:
        return False
    return True


def invoice_age_hours(inv: dict, now: datetime) -> float | None:
    created = parse_dt(inv.get("dateCreated")) or parse_dt(inv.get("dateIssued"))
    if not created:
        return None
    return (now - created).total_seconds() / 3600.0


def build_client_html(first: str, day: str, date: str, time_s: str, outstanding: float, signature_html: str) -> str:
    body = f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #222; line-height: 1.5;">
<p>Hey {first},</p>
<p>This is a friendly reminder of your upcoming appointment with Whisker Waggin' Mobile Veterinary Care on <strong>{day}, {date} at {time_s}</strong>.</p>
<p>Our system indicates you have not yet paid the outstanding balance on your account in the amount of <strong>{fmt_currency(outstanding)}</strong> for the appointment deposit. For your convenience and to keep your visit running smoothly, we kindly ask that all appointment deposits are paid prior to our arrival. If the deposit is not paid 24 hours prior to your visit, we will unfortunately need to cancel the trip.</p>
<p>You can view and pay your open invoice through the Pet Portal linked below. Our team can also take payment directly over the phone at the number below. If you have any questions or need assistance, please reach out to <a href="mailto:admin@whiskerwagginwnc.com">admin@whiskerwagginwnc.com</a> and our team would be happy to help.</p>
<p><a href="{PORTAL_URL}">{PORTAL_URL}</a></p>
<p>We look forward to caring for your pet soon!</p>
</div>
<br>
{signature_html}"""
    return body


def build_staff_html(
    report_date: str,
    sent_rows: list[dict],
    paid_rows: list[dict],
    signature_html: str,
    notes: list[str] | None = None,
) -> str:
    def table(headers: list[str], rows: list[list[str]]) -> str:
        th = "".join(f'<th style="border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;">{h}</th>' for h in headers)
        body_rows = []
        for row in rows:
            tds = "".join(f'<td style="border:1px solid #ccc;padding:6px 10px;vertical-align:top;">{c}</td>' for c in row)
            body_rows.append(f"<tr>{tds}</tr>")
        rows_html = "".join(body_rows)
        return (
            '<table style="border-collapse:collapse;font-size:13px;margin:8px 0 16px;">'
            f"<thead><tr>{th}</tr></thead><tbody>{rows_html}</tbody></table>"
        )

    parts = [
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">',
        f"<p><strong>[WWMVC] Outstanding Appointment Deposit Report — {report_date}</strong></p>",
        "<h3 style='margin-bottom:4px;'>Section 1 — Emails sent this run</h3>",
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

    parts.append("<h3 style='margin-bottom:4px;'>Section 2 — Paid since last report</h3>")
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

    if notes:
        parts.append("<h3 style='margin-bottom:4px;'>Notes (not emailed)</h3><ul>")
        for n in notes:
            parts.append(f"<li>{n}</li>")
        parts.append("</ul>")

    parts.append("</div><br>")
    parts.append(signature_html)
    return "\n".join(parts)


def get_composio():
    from composio import Composio

    return Composio(api_key=env("COMPOSIO_API_KEY"), toolkit_versions={"gmail": GMAIL_VERSION})


def gmail_execute(c, slug: str, arguments: dict) -> dict:
    return c.tools.execute(slug=slug, user_id=USER_ID, version=GMAIL_VERSION, arguments=arguments)


def load_signature(c) -> str:
    sig = gmail_execute(c, "GMAIL_LIST_SEND_AS", {})
    send_as = (sig.get("data") or {}).get("sendAs") or []
    if not send_as:
        raise RuntimeError("GMAIL_LIST_SEND_AS returned no sendAs entries")
    # Prefer primary / default
    primary = None
    for entry in send_as:
        if entry.get("isPrimary") or entry.get("isDefault"):
            primary = entry
            break
    entry = primary or send_as[0]
    signature = (entry.get("signature") or "").strip()
    if not signature:
        raise RuntimeError("Send-as signature empty; refusing to invent footer")
    return signature


def send_email(c, to: str, subject: str, html_body: str) -> dict:
    return gmail_execute(
        c,
        "GMAIL_SEND_EMAIL",
        {
            "recipient_email": to,
            "subject": subject,
            "body": html_body,
            "is_html": True,
        },
    )


def extract_message_id(result: dict) -> str:
    data = result.get("data") or {}
    return str(data.get("id") or data.get("messageId") or data.get("response_data", {}).get("id") or "")


def qualify(now: datetime) -> dict[str, Any]:
    date_from = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    appts = shepherd_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "embed": "client,appointmentStatus,appointmentType",
        },
    )
    invoices = shepherd_paginated(
        "open-api-invoices",
        {"embed": "invoiceStatus,invoiceItems,client"},
    )
    payments = shepherd_paginated(
        "open-api-payments",
        {"embed": "paymentMethod"},
    )

    # Credit by client: latest non-voided payment's clientBalanceAfterPayment
    credit_by_client: dict[str, float] = {}
    latest_pay_dt: dict[str, datetime] = {}
    for p in payments:
        if p.get("isDeleted") or p.get("isVoided") or p.get("voided"):
            continue
        cid = str(p.get("clientId") or "")
        if not cid:
            continue
        pdt = parse_dt(p.get("paymentDate")) or parse_dt(p.get("dateCreated"))
        if not pdt:
            continue
        if cid not in latest_pay_dt or pdt > latest_pay_dt[cid]:
            latest_pay_dt[cid] = pdt
            credit_by_client[cid] = money(p.get("clientBalanceAfterPayment"))

    # Active open invoices by client
    active_open: dict[str, list[dict]] = {}
    invoices_by_id: dict[str, dict] = {}
    for inv in invoices:
        invoices_by_id[str(inv.get("id"))] = inv
        if inv.get("isDeleted"):
            continue
        st = status_name(inv, "invoiceStatus")
        bal = money(inv.get("balance"))
        if st == "Active" and bal > 0:
            cid = str(inv.get("clientId") or "")
            if cid:
                active_open.setdefault(cid, []).append(inv)

    upcoming = [a for a in appts if is_upcoming_appt(a, now)]
    # Deduplicate: one appointment per client — pick soonest within 14d window candidates
    qualified: list[dict] = []
    exclusions: list[dict] = []
    seen_appt: set[str] = set()

    for appt in sorted(upcoming, key=lambda a: parse_dt(a.get("startDate")) or now):
        aid = str(appt.get("id"))
        if aid in seen_appt:
            continue
        seen_appt.add(aid)
        start = parse_dt(appt.get("startDate"))
        assert start
        ms = milestone_for(start, now)
        client = appt.get("client") if isinstance(appt.get("client"), dict) else None
        cid = str(appt.get("clientId") or (client or {}).get("id") or "")
        name = client_name(client)
        day, date_s, time_s = fmt_appt_local(start)
        appt_label = f"{day[:3]} {date_s} {time_s}"

        if not ms:
            continue  # outside 14d window — silent skip for report notes unless notable

        email = usable_email(client)
        if not email:
            # try fetch client
            if cid:
                try:
                    fetched = shepherd_paginated("open-api-clients", {"ids": cid})
                    if fetched:
                        client = fetched[0]
                        email = usable_email(client)
                        name = client_name(client)
                except Exception:
                    pass
        if not email:
            exclusions.append({"client": name, "reason": "no usable email", "appt": appt_label, "milestone": ms})
            continue

        credit = credit_by_client.get(cid, 0.0)
        if credit != 0.0:
            exclusions.append(
                {
                    "client": name,
                    "reason": f"credit {fmt_currency(credit)} (not exactly $0.00)",
                    "appt": appt_label,
                    "milestone": ms,
                }
            )
            continue

        opens = active_open.get(cid) or []
        deposit_invs = [i for i in opens if is_deposit_style(i)]
        if not deposit_invs:
            if opens:
                exclusions.append(
                    {
                        "client": name,
                        "reason": "Active open invoice(s) but not deposit-style / filtered AR",
                        "appt": appt_label,
                        "milestone": ms,
                        "invoices": [f"#{i.get('invoiceNumber') or i.get('number')} {fmt_currency(money(i.get('balance')))}" for i in opens],
                    }
                )
            else:
                exclusions.append(
                    {
                        "client": name,
                        "reason": "no Active outstanding deposit invoice",
                        "appt": appt_label,
                        "milestone": ms,
                    }
                )
            continue

        # Prefer oldest deposit invoice (most likely the appointment deposit)
        deposit_invs.sort(key=lambda i: parse_dt(i.get("dateCreated")) or parse_dt(i.get("dateIssued")) or now)
        inv = deposit_invs[0]
        age_h = invoice_age_hours(inv, now)
        inv_num = str(inv.get("invoiceNumber") or inv.get("number") or inv.get("id"))
        bal = money(inv.get("balance"))

        if age_h is None:
            exclusions.append(
                {
                    "client": name,
                    "reason": "invoice missing dateCreated/dateIssued",
                    "appt": appt_label,
                    "milestone": ms,
                    "invoice": f"#{inv_num} {fmt_currency(bal)}",
                }
            )
            continue
        if age_h <= 48.0:
            exclusions.append(
                {
                    "client": name,
                    "reason": f"invoice age ~{age_h:.1f}h ≤48h hold",
                    "appt": appt_label,
                    "milestone": ms,
                    "invoice": f"#{inv_num} {fmt_currency(bal)}",
                    "invoice_age_hours": round(age_h, 2),
                }
            )
            continue

        key = f"{aid}|{ms}"
        if key in SENT_MILESTONES:
            exclusions.append(
                {
                    "client": name,
                    "reason": f"{ms} already LIVE-sent",
                    "appt": appt_label,
                    "milestone": ms,
                    "invoice": f"#{inv_num} {fmt_currency(bal)}",
                }
            )
            continue

        qualified.append(
            {
                "appointment_id": aid,
                "client_id": cid,
                "client": name,
                "first_name": first_name(client),
                "email": email,
                "appointment": appt_label,
                "appointment_full": f"{day}, {date_s} at {time_s}",
                "day": day,
                "date": date_s,
                "time": time_s,
                "start_iso": start.isoformat(),
                "milestone": ms,
                "outstanding": bal,
                "outstanding_fmt": fmt_currency(bal),
                "credit": credit,
                "credit_fmt": fmt_currency(credit),
                "invoice": f"#{inv_num}",
                "invoice_id": str(inv.get("id")),
                "invoice_balance": bal,
                "invoice_age_hours": round(age_h, 2),
                "invoice_status": status_name(inv, "invoiceStatus"),
            }
        )

    # Paid since last report: prior tracked unpaid who no longer qualify as unpaid deposit
    paid_since: list[dict] = []
    for prior in PRIOR_TRACKED_UNPAID:
        pcid = str(prior.get("client_id") or "")
        pname = prior.get("client") or "(unknown)"
        credit = credit_by_client.get(pcid, 0.0)
        opens = active_open.get(pcid) or []
        deposit_open = [i for i in opens if is_deposit_style(i)]
        prior_inv_id = str(prior.get("invoice_id") or "")
        prior_inv = invoices_by_id.get(prior_inv_id) if prior_inv_id else None

        status_now = None
        evidence = None
        if credit > 0:
            status_now = "Paid / credit on account"
            evidence = f"Credit balance {fmt_currency(credit)}"
        elif prior_inv:
            st = status_name(prior_inv, "invoiceStatus")
            bal = money(prior_inv.get("balance"))
            if st != "Active" or bal <= 0:
                status_now = "Balance cleared" if bal <= 0 else "No longer Active open deposit"
                evidence = f"Invoice #{prior_inv.get('invoiceNumber') or prior_inv.get('number')} {st} {fmt_currency(bal)}"
        if status_now is None and not deposit_open:
            status_now = "No longer Active open deposit"
            evidence = "No Active open deposit-style invoice"
        if status_now:
            paid_since.append(
                {
                    "client": pname,
                    "appointment": prior.get("appointment") or "",
                    "prior_milestone": prior.get("milestone") or "",
                    "prior_outstanding": prior.get("outstanding_fmt") or "",
                    "status_now": status_now,
                    "evidence": evidence or "",
                }
            )

    return {
        "now_utc": now.isoformat(),
        "upcoming_count": len(upcoming),
        "qualified": qualified,
        "exclusions": exclusions,
        "paid_since": paid_since,
        "active_open_summary": {
            cid: [f"#{i.get('invoiceNumber') or i.get('number')} {fmt_currency(money(i.get('balance')))} {status_name(i,'invoiceStatus')}" for i in invs]
            for cid, invs in list(active_open.items())[:50]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualify-only", action="store_true")
    parser.add_argument("--live", action="store_true", help="SEND client emails + staff report")
    args = parser.parse_args()

    now = datetime.now(UTC)
    try:
        result = qualify(now)
    except Exception as e:
        print(json.dumps({"error": "shepherd_failed", "detail": str(e)}), file=sys.stderr)
        if args.live:
            try:
                c = get_composio()
                sig = load_signature(c)
                body = f"<p>Deposit reminder run failed during Shepherd qualification.</p><p>{e}</p><br>{sig}"
                send_email(c, FAIL_TO, "[WWMVC] Deposit Reminder Failure", body)
            except Exception as ge:
                print(json.dumps({"error": "failure_email_failed", "detail": str(ge)}), file=sys.stderr)
        return 1

    out_path = "/tmp/deposit_reminder_qualify.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {out_path}")
    print(f"Qualified: {len(result['qualified'])}")
    for q in result["qualified"]:
        print(f"  - {q['client']} <{q['email']}> {q['appointment']} {q['milestone']} {q['outstanding_fmt']} inv {q['invoice']} age={q['invoice_age_hours']}h")
    print(f"Exclusions: {len(result['exclusions'])}")
    for ex in result["exclusions"][:40]:
        print(f"  - {ex.get('client')}: {ex.get('reason')} ({ex.get('appt')}, {ex.get('milestone')})")

    if args.qualify_only or not args.live:
        return 0

    # LIVE send
    try:
        c = get_composio()
        signature_html = load_signature(c)
    except Exception as e:
        print(json.dumps({"error": "gmail_signature_failed", "detail": str(e)}), file=sys.stderr)
        try:
            c = get_composio()
            # cannot load signature — still try failure note without invented footer? Instructions say fail Gmail step.
            # Attempt plain failure to nmarquette without signature if signature failed.
            send_email(
                c,
                FAIL_TO,
                "[WWMVC] Deposit Reminder Failure",
                f"<p>Deposit reminder Gmail signature load failed: {e}</p><p>Client emails and staff report were NOT sent.</p>",
            )
        except Exception as ge:
            print(json.dumps({"error": "failure_email_failed", "detail": str(ge)}), file=sys.stderr)
        return 1

    sent_rows: list[dict] = []
    send_results: list[dict] = []
    for q in result["qualified"]:
        html = build_client_html(
            q["first_name"], q["day"], q["date"], q["time"], q["outstanding"], signature_html
        )
        try:
            r = send_email(c, q["email"], CLIENT_SUBJECT, html)
            mid = extract_message_id(r)
            send_results.append({"ok": True, "client": q["client"], "message_id": mid, "raw_keys": list((r.get("data") or {}).keys())})
            sent_rows.append(
                {
                    "client": q["client"],
                    "email": q["email"],
                    "appointment": q["appointment_full"],
                    "milestone": q["milestone"],
                    "outstanding": q["outstanding_fmt"],
                    "credit": q["credit_fmt"],
                    "invoice": q["invoice"],
                    "appointment_id": q["appointment_id"],
                    "message_id": mid,
                }
            )
            print(f"SENT {q['client']} {q['milestone']} id={mid}")
        except Exception as e:
            send_results.append({"ok": False, "client": q["client"], "error": str(e)})
            print(f"FAILED send to {q['client']}: {e}", file=sys.stderr)

    report_date = now.astimezone(TZ).strftime("%B %-d, %Y").replace(" 0", " ")
    # %-d may not work on all platforms; fallback:
    local = now.astimezone(TZ)
    report_date = f"{local.strftime('%B')} {local.day}, {local.year}"

    notes = []
    for ex in result["exclusions"]:
        if any(k in (ex.get("reason") or "") for k in ("invoice age", "credit", "already LIVE", "deposit-style", "filtered")):
            notes.append(f"{ex.get('client')} — {ex.get('appt')} ({ex.get('milestone')}): {ex.get('reason')}" + (f" [{ex.get('invoice')}]" if ex.get("invoice") else ""))

    staff_html = build_staff_html(report_date, sent_rows, result["paid_since"], signature_html, notes=notes[:25])
    staff_subject = f"[WWMVC] Outstanding Appointment Deposit Report — {report_date}"
    try:
        sr = send_email(c, STAFF_TO, staff_subject, staff_html)
        staff_mid = extract_message_id(sr)
        print(f"STAFF REPORT SENT id={staff_mid} subject={staff_subject}")
    except Exception as e:
        print(f"STAFF REPORT FAILED: {e}", file=sys.stderr)
        try:
            send_email(
                c,
                FAIL_TO,
                "[WWMVC] Deposit Reminder Failure",
                f"<p>Staff report send failed: {e}</p><p>Client sends attempted: {len(sent_rows)}</p><br>{signature_html}",
            )
        except Exception:
            pass
        return 1

    summary = {
        "sent": sent_rows,
        "send_results": send_results,
        "staff_message_id": staff_mid,
        "staff_subject": staff_subject,
        "paid_since": result["paid_since"],
        "exclusions": result["exclusions"],
        "qualified_count": len(result["qualified"]),
        "signature_chars": len(signature_html),
    }
    with open("/tmp/deposit_reminder_live_result.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("Wrote /tmp/deposit_reminder_live_result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
