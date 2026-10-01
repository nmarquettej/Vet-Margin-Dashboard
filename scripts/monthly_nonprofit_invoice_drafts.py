#!/usr/bin/env python3
"""Monthly Nonprofit AR invoice Gmail drafts (TEST MODE: drafts only).

Candidates: Shepherd Client Discount = Nonprofit discount OR Client Type = Nonprofit.
Draft only when reporting-month invoices exist AND an open Active balance > $0 remains.
Client emails are Gmail drafts (never sent). Staff report is always sent on send days.

Send-day gate (America/New_York):
  1) Today is the 1st AND Mon–Fri, OR
  2) Today is Monday AND the 1st of this month fell on Sat/Sun

Override with --force for ad-hoc / test runs.
Attachments: never download from Shepherd; drafts have no PDF.
Staff report marks Attachment as "Manual — staff to attach PDF".
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from composio import Composio

ET = ZoneInfo("America/New_York")
USER_ID = "noreply@whiskerwagginwnc.com"
GMAIL_ACCOUNT_ID = "ca_Gov3XV3vXXnN"
GMAIL_TOOLKIT_VERSION = "20260721_00"
STAFF_TO = "nmarquettej@gmail.com"
STAFF_CC = ["courtney@whiskerwagginwnc.com"]

NONPROFIT_DISCOUNT_IDS = {
    "842e0216-2ded-4460-a8ea-b3e1012fc524",  # active
    "c054013c-d4d6-4aa2-b647-b34a0174a425",  # inactive duplicate name
}
NONPROFIT_CLIENT_TYPE_NAMES = {"nonprofit"}

# (match substrings against Shepherd client name) -> greet, org display for subject/body
ORG_MAP: list[tuple[tuple[str, ...], str, str]] = [
    (("feline urgent rescue", " fur", "fur of wnc"), "Syd", "FUR of WNC"),
    (("sarge",), "Windy", "Sarge's"),
    (("bryson city cat",), "Hannah", "Bryson City Cat Sanctuary"),
    (("swain county",), "Pam", "Swain County Animal Shelter"),
    (("misfit mountain",), "", "Misfit Mountain Animal Rescue"),  # greet from primary contact
]

TRACKING_PATH = Path(__file__).resolve().parent / ".nonprofit_ar_draft_tracking.json"


@dataclass
class NonprofitClient:
    id: str
    shepherd_name: str
    first_name: str | None
    email: str | None
    greet: str | None
    org_display: str
    missing_greet: bool = False
    source: str = ""  # discount / type / both


@dataclass
class QualifyingDraft:
    client: NonprofitClient
    month_invoice_total: float
    open_active_due: float
    active_invoice_numbers: list[str] = field(default_factory=list)
    draft_id: str | None = None
    ok: bool = False
    error: str | None = None
    notes: str = ""


@dataclass
class SkippedNote:
    client: NonprofitClient
    month_invoice_total: float
    reason: str


def is_send_day(today: date) -> bool:
    """Business-day rule for the monthly nonprofit AR workflow."""
    if today.weekday() >= 5:  # Sat/Sun
        return False
    first = today.replace(day=1)
    if today.day == 1 and today.weekday() < 5:
        return True
    if today.weekday() == 0 and first.weekday() >= 5:
        return True
    return False


def prior_month(today: date) -> tuple[int, int, str]:
    if today.month == 1:
        return today.year - 1, 12, "December"
    month = today.month - 1
    name = datetime(today.year, month, 1).strftime("%B")
    return today.year, month, name


def shepherd_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Integration-Public-Key": os.environ["SHEPHERD_PUBLIC_KEY"],
        "X-Integration-Private-Key": os.environ["SHEPHERD_PRIVATE_KEY"],
        "X-Clinic-Id": os.environ["SHEPHERD_CLINIC_ID"],
    }


def shepherd_post(endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = os.environ["SHEPHERD_BASE_URL"].rstrip("/")
    response = httpx.post(
        f"{base}/{endpoint.lstrip('/')}",
        headers=shepherd_headers(),
        json=body or {},
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()


def shepherd_paginated(endpoint: str, body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    page = 1
    items: list[dict[str, Any]] = []
    base_body = dict(body or {})
    while True:
        data = shepherd_post(endpoint, {**base_body, "page": page, "rpp": 500})
        batch = data.get("item") or data.get("items") or []
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        total = data.get("totalRecords") or data.get("total_records") or len(items)
        if len(items) >= total:
            break
        page += 1
    return items


def map_org(shepherd_name: str, first_name: str | None) -> tuple[str | None, str, bool]:
    lowered = f" {shepherd_name.lower()} "
    for needles, greet, org in ORG_MAP:
        if any(n in lowered or n in shepherd_name.lower() for n in needles):
            if greet:
                return greet, org, False
            # Mapped org but greet from contact on file (e.g. Misfit Mountain)
            if first_name and first_name.strip() and first_name.strip().lower() not in org.lower():
                return first_name.strip().split()[0], org, False
            if first_name and first_name.strip():
                # firstName looks like org name — flag missing personal greet
                return first_name.strip().split()[0], org, True
            return None, org, True
    if first_name and first_name.strip():
        return first_name.strip().split()[0], shepherd_name, False
    return None, shepherd_name, True


def client_email(client: dict[str, Any]) -> str | None:
    for key in ("email", "emailAddress", "primaryEmail"):
        value = client.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return None


def parse_issued(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def invoice_total(inv: dict[str, Any]) -> float:
    raw = inv.get("total")
    if raw is None:
        raw = inv.get("totalAmount")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def invoice_balance(inv: dict[str, Any]) -> float:
    try:
        return float(inv.get("balance") or 0)
    except (TypeError, ValueError):
        return 0.0


def invoice_status_name(inv: dict[str, Any]) -> str:
    return (inv.get("invoiceStatus") or {}).get("name") or str(inv.get("status") or "")


def is_open_active_unpaid(inv: dict[str, Any]) -> bool:
    """Active status with outstanding balance > $0."""
    if invoice_status_name(inv).strip().lower() != "active":
        return False
    return invoice_balance(inv) > 0.0


def is_nonprofit_candidate(client: dict[str, Any]) -> tuple[bool, str]:
    discount_id = client.get("clientDiscountId") or (client.get("clientDiscount") or {}).get("id")
    has_discount = discount_id in NONPROFIT_DISCOUNT_IDS
    ct = client.get("clientType") or {}
    type_name = ""
    if isinstance(ct, dict):
        type_name = (ct.get("name") or "").strip()
    elif isinstance(ct, str):
        type_name = ct.strip()
    has_type = type_name.lower() in NONPROFIT_CLIENT_TYPE_NAMES
    if has_discount and has_type:
        return True, "discount+type"
    if has_discount:
        return True, "discount"
    if has_type:
        return True, "type"
    return False, ""


def load_nonprofit_clients() -> list[NonprofitClient]:
    clients = shepherd_paginated("open-api-clients", {"embed": "clientDiscount,clientType"})
    out: list[NonprofitClient] = []
    for client in clients:
        ok, source = is_nonprofit_candidate(client)
        if not ok:
            continue
        name = (
            client.get("name")
            or f"{client.get('firstName') or ''} {client.get('lastName') or ''}".strip()
            or "(unnamed)"
        )
        first = client.get("firstName")
        greet, org, missing = map_org(name, first if isinstance(first, str) else None)
        # If greet missing, use best available name for draft (flag in report)
        if not greet:
            last = client.get("lastName")
            if isinstance(first, str) and first.strip():
                greet = first.strip().split()[0]
                missing = True
            elif isinstance(last, str) and last.strip():
                greet = last.strip().split()[0]
                missing = True
            else:
                greet = org.split()[0] if org else "there"
                missing = True
        out.append(
            NonprofitClient(
                id=str(client["id"]),
                shepherd_name=name,
                first_name=first if isinstance(first, str) else None,
                email=client_email(client),
                greet=greet,
                org_display=org,
                missing_greet=missing,
                source=source,
            )
        )
    return out


def load_prior_month_invoices(year: int, month: int) -> list[dict[str, Any]]:
    # Shepherd date filters are unreliable — fetch a broad window and filter locally.
    if month == 1:
        start = datetime(year - 1, 12, 1, tzinfo=ET)
    else:
        start = datetime(year, month - 1, 15, tzinfo=ET)
    if month == 12:
        end = datetime(year + 1, 1, 15, tzinfo=ET)
    else:
        end = datetime(year, month + 1, 15, tzinfo=ET)

    invoices = shepherd_paginated(
        "open-api-invoices",
        {
            "isDeleted": False,
            "embed": "invoiceStatus,client,invoiceItems",
            "dateIssuedFrom": start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dateIssuedTo": end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
    )
    filtered: list[dict[str, Any]] = []
    for inv in invoices:
        issued = parse_issued(inv.get("dateIssued"))
        if not issued:
            continue
        local = issued.astimezone(ET)
        if local.year == year and local.month == month:
            filtered.append(inv)
    return filtered


def load_tracking() -> dict[str, Any]:
    if TRACKING_PATH.exists():
        try:
            return json.loads(TRACKING_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {"drafted": []}
    return {"drafted": []}


def save_tracking(data: dict[str, Any]) -> None:
    TRACKING_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def tracking_key(org_display: str, year: int, month: int) -> str:
    return f"{org_display.strip().lower()}|{year}-{month:02d}"


def exact_client_body(first_name: str, org_name: str, weekday: str, month_name: str) -> str:
    """Copy EXACT body; only fill bracketed placeholders."""
    return (
        f"Hey {first_name},\n\n"
        f"Happy {weekday}!\n\n"
        f"We have attached {org_name}'s {month_name} invoice for services provided. "
        f"Let us know if you have any questions, and our team is happy to connect.\n\n"
        f"Have a great day!"
    )


def text_to_html_paras(text: str) -> str:
    parts = text.split("\n\n")
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in parts)


def gmail_execute(composio: Composio, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    resp = composio.tools.execute(
        slug=slug,
        arguments=arguments,
        connected_account_id=GMAIL_ACCOUNT_ID,
        user_id=USER_ID,
        version=GMAIL_TOOLKIT_VERSION,
    )
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if isinstance(resp, dict):
        return resp
    return {"successful": False, "error": str(resp)}


def load_noreply_signature(composio: Composio) -> str:
    resp = gmail_execute(composio, "GMAIL_LIST_SEND_AS", {"user_id": "me"})
    if not resp.get("successful"):
        raise RuntimeError(f"GMAIL_LIST_SEND_AS failed: {resp.get('error')}")
    aliases = (resp.get("data") or {}).get("sendAs") or []
    for alias in aliases:
        if alias.get("sendAsEmail", "").lower() == USER_ID.lower() and alias.get("signature"):
            return alias["signature"]
    if aliases and aliases[0].get("signature"):
        return aliases[0]["signature"]
    raise RuntimeError("NoReply Signature HTML missing — aborting (never invent footer)")


def create_draft(
    composio: Composio,
    *,
    to_email: str,
    subject: str,
    body_html: str,
) -> dict[str, Any]:
    return gmail_execute(
        composio,
        "GMAIL_CREATE_EMAIL_DRAFT",
        {
            "recipient_email": to_email,
            "subject": subject,
            "body": body_html,
            "is_html": True,
            "user_id": "me",
        },
    )


def send_staff_report(composio: Composio, *, subject: str, body_html: str) -> dict[str, Any]:
    return gmail_execute(
        composio,
        "GMAIL_SEND_EMAIL",
        {
            "recipient_email": STAFF_TO,
            "cc": STAFF_CC,
            "subject": subject,
            "body": body_html,
            "is_html": True,
            "user_id": "me",
            "from_email": USER_ID,
        },
    )


def send_failure_note(composio: Composio, message: str) -> None:
    try:
        gmail_execute(
            composio,
            "GMAIL_SEND_EMAIL",
            {
                "recipient_email": STAFF_TO,
                "subject": "[WWMVC] Monthly Nonprofit AR Invoice Report — FAILURE",
                "body": f"<p>{html.escape(message)}</p>",
                "is_html": True,
                "user_id": "me",
                "from_email": USER_ID,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not send failure note: {exc}", file=sys.stderr)


def build_staff_html(
    *,
    today: date,
    month_name: str,
    year: int,
    weekday: str,
    forced: bool,
    drafts: list[QualifyingDraft],
    skipped: list[SkippedNote],
    not_considered: list[str],
    signature: str,
) -> str:
    lines: list[str] = [
        f"<h2>Monthly Nonprofit AR Invoice Report — {html.escape(today.isoformat())}</h2>",
        "<p><b>Mode:</b> TEST — client emails are DRAFTS only (not sent).<br>",
        f"<b>Reporting month:</b> {html.escape(month_name)} {year}<br>",
        f"<b>Forced run:</b> {'Yes' if forced else 'No'}<br>",
        f"<b>Run day greeting:</b> {html.escape(weekday)}<br>",
        f"<b>From:</b> {html.escape(USER_ID)} + NoReply Signature<br>",
        "<b>Attachment policy:</b> Manual — staff to attach PDF (no Shepherd download).</p>",
        "<h3>Section 1 — Drafts created this run</h3>",
    ]
    ok_drafts = [d for d in drafts if d.ok]
    if not ok_drafts and not drafts:
        lines.append("<p>No qualifying unpaid nonprofit AR invoices this run.</p>")
    else:
        lines.extend(
            [
                "<table border='1' cellpadding='6' cellspacing='0'>",
                "<tr><th>Client / Org</th><th>Contact</th><th>Email</th>"
                "<th>Invoice month</th><th>Open amount due</th>"
                "<th>Attachment</th><th>Notes</th></tr>",
            ]
        )
        for d in drafts:
            notes = d.notes or ("OK" if d.ok else f"FAIL: {d.error or 'unknown'}")
            if d.draft_id:
                notes = f"{notes}; draft id {d.draft_id}"
            if d.active_invoice_numbers:
                notes = f"Active inv #{', #'.join(d.active_invoice_numbers)}; {notes}"
            lines.append(
                "<tr>"
                f"<td>{html.escape(d.client.org_display)}<br>"
                f"<small>{html.escape(d.client.shepherd_name)}</small></td>"
                f"<td>{html.escape(d.client.greet or 'MISSING')}</td>"
                f"<td>{html.escape(d.client.email or '')}</td>"
                f"<td>{html.escape(month_name)}</td>"
                f"<td>${d.open_active_due:,.2f}</td>"
                "<td>Manual — staff to attach PDF</td>"
                f"<td>{html.escape(notes)}</td>"
                "</tr>"
            )
        lines.append("</table>")
        if not ok_drafts:
            lines.append("<p>No qualifying unpaid nonprofit AR invoices this run.</p>")

    lines.append("<h3>Section 2 — Skipped / Notes</h3>")
    if not skipped:
        lines.append("<p>None</p>")
    else:
        lines.extend(
            [
                "<table border='1' cellpadding='6' cellspacing='0'>",
                "<tr><th>Client / Org</th><th>Contact</th><th>Reporting month</th>"
                "<th>Total invoices that month</th><th>Reason skipped</th></tr>",
            ]
        )
        for s in skipped:
            lines.append(
                "<tr>"
                f"<td>{html.escape(s.client.org_display)}<br>"
                f"<small>{html.escape(s.client.shepherd_name)}</small></td>"
                f"<td>{html.escape(s.client.greet or s.client.first_name or '—')}</td>"
                f"<td>{html.escape(month_name)}</td>"
                f"<td>${s.month_invoice_total:,.2f}</td>"
                f"<td>{html.escape(s.reason)}</td>"
                "</tr>"
            )
        lines.append("</table>")

    if not_considered:
        lines.append("<h3>Section 3 — Not considered</h3><ul>")
        for note in not_considered:
            lines.append(f"<li>{html.escape(note)}</li>")
        lines.append("</ul>")

    return "\n".join(lines) + "<br>" + signature


def qualify_clients(
    nonprofits: list[NonprofitClient],
    by_client: dict[str, list[dict[str, Any]]],
    *,
    year: int,
    month: int,
    month_name: str,
    already_drafted: set[str],
) -> tuple[list[QualifyingDraft], list[SkippedNote], list[str]]:
    drafts: list[QualifyingDraft] = []
    skipped: list[SkippedNote] = []
    not_considered: list[str] = []

    for client in nonprofits:
        matches = by_client.get(client.id, [])
        month_total = sum(invoice_total(inv) for inv in matches)

        if not matches:
            not_considered.append(
                f"{client.shepherd_name} — Nonprofit candidate ({client.source}); "
                f"no {month_name} invoices"
            )
            continue

        active_unpaid = [inv for inv in matches if is_open_active_unpaid(inv)]
        open_due = sum(invoice_balance(inv) for inv in active_unpaid)
        reporting_month_fully_settled = all(
            invoice_balance(inv) <= 0.0 or inv.get("isPaid") is True for inv in matches
        ) and not active_unpaid

        tkey = tracking_key(client.org_display, year, month)
        if tkey in already_drafted:
            skipped.append(
                SkippedNote(
                    client=client,
                    month_invoice_total=month_total,
                    reason=(
                        f"Already drafted for {month_name} {year} "
                        "(clear tracking to retest)"
                    ),
                )
            )
            continue

        if not active_unpaid or open_due <= 0.0:
            reason = "Paid in full — no open Active balance"
            if matches and not reporting_month_fully_settled:
                # Has unpaid non-Active (e.g. Charges Complete) but no Active open
                statuses = sorted({invoice_status_name(i) or "?" for i in matches})
                reason = (
                    "No open Active unpaid balance "
                    f"(reporting-month statuses: {', '.join(statuses)}; "
                    f"month invoice total ${month_total:,.2f})"
                )
            skipped.append(
                SkippedNote(
                    client=client,
                    month_invoice_total=month_total,
                    reason=reason,
                )
            )
            continue

        if not client.email:
            skipped.append(
                SkippedNote(
                    client=client,
                    month_invoice_total=month_total,
                    reason="Missing usable email on file — draft not created",
                )
            )
            continue

        notes = ""
        if client.missing_greet:
            notes = (
                f"Contact first name missing/ambiguous on file; "
                f"used best available greet '{client.greet}'"
            )

        drafts.append(
            QualifyingDraft(
                client=client,
                month_invoice_total=month_total,
                open_active_due=open_due,
                active_invoice_numbers=[
                    str(inv.get("invoiceNumber") or inv.get("number") or "?")
                    for inv in active_unpaid
                ],
                notes=notes,
            )
        )

    return drafts, skipped, not_considered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when today is not a calendar send day",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Qualify clients/invoices only; do not create drafts or send staff report",
    )
    parser.add_argument(
        "--clear-tracking",
        action="store_true",
        help="Clear draft tracking before qualifying (retest)",
    )
    args = parser.parse_args()

    today = datetime.now(ET).date()
    weekday = today.strftime("%A")
    year, month, month_name = prior_month(today)

    if not args.force and not is_send_day(today):
        print(
            f"SKIP: {today.isoformat()} ({weekday}) is not a send day "
            f"(1st Mon–Fri, or Monday after weekend 1st). Use --force to override."
        )
        return 0

    if not os.environ.get("COMPOSIO_API_KEY") and not args.dry_run:
        print("COMPOSIO_API_KEY is required", file=sys.stderr)
        return 1
    for key in ("SHEPHERD_PUBLIC_KEY", "SHEPHERD_PRIVATE_KEY", "SHEPHERD_CLINIC_ID", "SHEPHERD_BASE_URL"):
        if not os.environ.get(key):
            print(f"{key} is required", file=sys.stderr)
            return 1

    if args.clear_tracking:
        save_tracking({"drafted": []})
        print("Cleared draft tracking")

    tracking = load_tracking()
    already = {str(x) for x in tracking.get("drafted") or []}

    nonprofits = load_nonprofit_clients()
    invoices = load_prior_month_invoices(year, month)
    by_client: dict[str, list[dict[str, Any]]] = {}
    for inv in invoices:
        client = inv.get("client") or {}
        cid = str(inv.get("clientId") or client.get("id") or "")
        if not cid:
            continue
        by_client.setdefault(cid, []).append(inv)

    drafts, skipped, not_considered = qualify_clients(
        nonprofits,
        by_client,
        year=year,
        month=month,
        month_name=month_name,
        already_drafted=already,
    )

    summary = {
        "today": today.isoformat(),
        "weekday": weekday,
        "prior_month": month_name,
        "prior_year": year,
        "forced": bool(args.force),
        "send_day": is_send_day(today),
        "nonprofit_count": len(nonprofits),
        "qualifying": len(drafts),
        "skipped": len(skipped),
        "not_considered": len(not_considered),
        "drafts": [
            {
                "org": d.client.org_display,
                "greet": d.client.greet,
                "email": d.client.email,
                "open_active_due": d.open_active_due,
                "month_total": d.month_invoice_total,
                "active_invoices": d.active_invoice_numbers,
                "notes": d.notes,
            }
            for d in drafts
        ],
        "skipped_detail": [
            {
                "org": s.client.org_display,
                "month_total": s.month_invoice_total,
                "reason": s.reason,
            }
            for s in skipped
        ],
        "not_considered_detail": not_considered,
    }
    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print("DRY RUN — no drafts / no staff report")
        return 0

    composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    try:
        signature = load_noreply_signature(composio)
        if not signature.strip():
            raise RuntimeError("Empty signature")
    except Exception as exc:  # noqa: BLE001
        send_failure_note(composio, f"Signature load failed: {exc}")
        print(f"FATAL signature: {exc}", file=sys.stderr)
        return 1

    for d in drafts:
        subject = f"[Whisker Waggin' MVC] {d.client.org_display} {month_name} Invoice"
        body_text = exact_client_body(d.client.greet or "", d.client.org_display, weekday, month_name)
        body_html = text_to_html_paras(body_text) + "<br>" + signature
        try:
            resp = create_draft(
                composio,
                to_email=d.client.email or "",
                subject=subject,
                body_html=body_html,
            )
        except Exception as exc:  # noqa: BLE001
            d.ok = False
            d.error = str(exc)
            print(f"DRAFT FAIL {d.client.org_display}: {exc}")
            continue
        d.ok = bool(resp.get("successful"))
        d.error = resp.get("error")
        data = resp.get("data") or {}
        d.draft_id = data.get("id") or data.get("draftId") or data.get("draft_id")
        print(
            f"DRAFT {d.client.org_display}: ok={d.ok} id={d.draft_id} "
            f"open=${d.open_active_due:.2f} attachment=Manual — staff to attach PDF"
        )
        if d.ok:
            already.add(tracking_key(d.client.org_display, year, month))

    tracking["drafted"] = sorted(already)
    tracking["last_run"] = {
        "date": today.isoformat(),
        "reporting_month": f"{year}-{month:02d}",
        "drafts_ok": sum(1 for d in drafts if d.ok),
    }
    save_tracking(tracking)

    staff_subject = f"[WWMVC] Monthly Nonprofit AR Invoice Report — {today.isoformat()}"
    staff_body = build_staff_html(
        today=today,
        month_name=month_name,
        year=year,
        weekday=weekday,
        forced=bool(args.force),
        drafts=drafts,
        skipped=skipped,
        not_considered=not_considered,
        signature=signature,
    )
    try:
        staff = send_staff_report(composio, subject=staff_subject, body_html=staff_body)
    except Exception as exc:  # noqa: BLE001
        print(f"STAFF REPORT FAIL: {exc}", file=sys.stderr)
        send_failure_note(composio, f"Staff report send failed after drafts: {exc}")
        return 2

    print(
        "STAFF REPORT:",
        "ok=",
        staff.get("successful"),
        "id=",
        (staff.get("data") or {}).get("id"),
        "url=",
        (staff.get("data") or {}).get("display_url"),
    )
    if not staff.get("successful"):
        send_failure_note(
            composio,
            f"Staff report send returned unsuccessful: {staff.get('error')}",
        )
        return 2
    return 0 if all(d.ok for d in drafts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
