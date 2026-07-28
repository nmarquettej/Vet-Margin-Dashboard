#!/usr/bin/env python3
"""Monthly Nonprofit AR invoice Gmail drafts (TEST MODE: drafts only).

Qualifying clients: Shepherd Client Discount = Nonprofit discount, plus a prior-
calendar-month invoice with total > $0. Client emails are created as Gmail drafts
(never sent). Staff report is always sent.

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
from dataclasses import dataclass
from datetime import date, datetime
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

# (match substrings against Shepherd client name) -> greet, org display for subject/body
ORG_MAP: list[tuple[tuple[str, ...], str, str]] = [
    (("feline urgent rescue", " fur", "fur of wnc"), "Syd", "FUR of WNC"),
    (("sarge",), "Windy", "Sarge's"),
    (("bryson city cat",), "Hannah", "Bryson City Cat Sanctuary"),
    (("swain county",), "Pam", "Swain County Animal Shelter"),
]


@dataclass
class NonprofitClient:
    id: str
    shepherd_name: str
    first_name: str | None
    email: str | None
    greet: str | None
    org_display: str
    missing_greet: bool = False


@dataclass
class QualifyingDraft:
    client: NonprofitClient
    invoice_id: str
    invoice_number: Any
    total: float
    status: str
    date_issued: str
    draft_id: str | None = None
    ok: bool = False
    error: str | None = None


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
            return greet, org, False
    if first_name and first_name.strip():
        return first_name.strip(), shepherd_name, False
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


def load_nonprofit_clients() -> list[NonprofitClient]:
    clients = shepherd_paginated("open-api-clients", {"embed": "clientDiscount"})
    out: list[NonprofitClient] = []
    for client in clients:
        discount_id = client.get("clientDiscountId") or (client.get("clientDiscount") or {}).get("id")
        if discount_id not in NONPROFIT_DISCOUNT_IDS:
            continue
        name = (
            client.get("name")
            or f"{client.get('firstName') or ''} {client.get('lastName') or ''}".strip()
            or "(unnamed)"
        )
        first = client.get("firstName")
        greet, org, missing = map_org(name, first if isinstance(first, str) else None)
        out.append(
            NonprofitClient(
                id=str(client["id"]),
                shepherd_name=name,
                first_name=first if isinstance(first, str) else None,
                email=client_email(client),
                greet=greet,
                org_display=org,
                missing_greet=missing,
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


def build_staff_html(
    *,
    month_name: str,
    weekday: str,
    forced: bool,
    drafts: list[QualifyingDraft],
    skipped: list[str],
    signature: str,
) -> str:
    lines: list[str] = [
        f"<h2>Nonprofit AR Invoice Drafts — {html.escape(month_name)} 2026 (TEST MODE)</h2>",
        "<p><b>Mode:</b> Client emails are DRAFTS only — not sent.<br>",
        f"<b>Forced run:</b> {'Yes' if forced else 'No'}<br>",
        f"<b>Run day greeting:</b> {html.escape(weekday)}<br>",
        f"<b>From:</b> {html.escape(USER_ID)} + NoReply Signature<br>",
        "<b>Attachment policy:</b> Manual — staff to attach PDF (no Shepherd download).</p>",
        f"<p><b>Drafts created:</b> {sum(1 for d in drafts if d.ok)} / {len(drafts)}</p>",
        "<table border='1' cellpadding='6' cellspacing='0'>",
        "<tr><th>Org</th><th>To</th><th>Greet</th><th>Subject</th><th>Invoice</th>"
        "<th>Total</th><th>Draft ID</th><th>Attachment</th><th>Status</th></tr>",
    ]
    for d in drafts:
        subject = f"[Whisker Waggin' MVC] {d.client.org_display} {month_name} Invoice"
        status = "OK" if d.ok else f"FAIL: {d.error or 'unknown'}"
        lines.append(
            "<tr>"
            f"<td>{html.escape(d.client.org_display)}<br><small>{html.escape(d.client.shepherd_name)}</small></td>"
            f"<td>{html.escape(d.client.email or '')}</td>"
            f"<td>{html.escape(d.client.greet or 'MISSING')}</td>"
            f"<td>{html.escape(subject)}</td>"
            f"<td>#{html.escape(str(d.invoice_number))} ({html.escape(d.status)})</td>"
            f"<td>${d.total:,.2f}</td>"
            f"<td>{html.escape(str(d.draft_id or '—'))}</td>"
            "<td>Manual — staff to attach PDF</td>"
            f"<td>{html.escape(status)}</td>"
            "</tr>"
        )
    lines.append("</table>")
    lines.append("<h3>Skipped / notes</h3><ul>")
    if not skipped:
        lines.append("<li>None</li>")
    else:
        for note in skipped:
            lines.append(f"<li>{note}</li>")
    lines.append("</ul>")
    return "\n".join(lines) + "<br>" + signature


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

    nonprofits = load_nonprofit_clients()
    invoices = load_prior_month_invoices(year, month)
    by_client: dict[str, list[dict[str, Any]]] = {}
    for inv in invoices:
        client = inv.get("client") or {}
        cid = str(inv.get("clientId") or client.get("id") or "")
        if not cid:
            continue
        by_client.setdefault(cid, []).append(inv)

    drafts: list[QualifyingDraft] = []
    skipped: list[str] = []

    for client in nonprofits:
        matches = [
            inv
            for inv in by_client.get(client.id, [])
            if invoice_total(inv) > 0
        ]
        if not matches:
            skipped.append(
                f"<b>{html.escape(client.shepherd_name)}</b> — Nonprofit discount present; "
                f"no {html.escape(month_name)} invoice with total &gt; $0"
                + (
                    f" (would greet {html.escape(client.greet)} / {html.escape(client.org_display)})."
                    if client.greet
                    else "."
                )
            )
            continue
        if not client.email:
            skipped.append(
                f"<b>{html.escape(client.shepherd_name)}</b> — qualifying invoice(s) found but "
                "no email on file."
            )
            continue
        if client.missing_greet or not client.greet:
            skipped.append(
                f"<b>{html.escape(client.shepherd_name)}</b> — qualifying invoice found but "
                "contact first name missing on file; draft not created."
            )
            continue

        # Prefer a single representative invoice (highest total) for the draft wording
        inv = max(matches, key=invoice_total)
        status = (inv.get("invoiceStatus") or {}).get("name") or str(inv.get("status") or "")
        drafts.append(
            QualifyingDraft(
                client=client,
                invoice_id=str(inv.get("id")),
                invoice_number=inv.get("invoiceNumber") or inv.get("number"),
                total=invoice_total(inv),
                status=status,
                date_issued=str(inv.get("dateIssued") or ""),
            )
        )

    summary = {
        "today": today.isoformat(),
        "weekday": weekday,
        "prior_month": month_name,
        "forced": bool(args.force),
        "nonprofit_count": len(nonprofits),
        "qualifying": len(drafts),
        "skipped": len(skipped),
    }
    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print("DRY RUN — no drafts / no staff report")
        for d in drafts:
            print(
                f"  WOULD DRAFT: {d.client.org_display} -> {d.client.greet} "
                f"<{d.client.email}> invoice #{d.invoice_number} ${d.total:.2f}"
            )
        return 0

    composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    signature = load_noreply_signature(composio)
    if not signature.strip():
        raise RuntimeError("Empty signature")

    for d in drafts:
        subject = f"[Whisker Waggin' MVC] {d.client.org_display} {month_name} Invoice"
        body_text = exact_client_body(d.client.greet or "", d.client.org_display, weekday, month_name)
        body_html = text_to_html_paras(body_text) + "<br>" + signature
        resp = create_draft(
            composio,
            to_email=d.client.email or "",
            subject=subject,
            body_html=body_html,
        )
        d.ok = bool(resp.get("successful"))
        d.error = resp.get("error")
        data = resp.get("data") or {}
        d.draft_id = data.get("id") or data.get("draftId") or data.get("draft_id")
        print(
            f"DRAFT {d.client.org_display}: ok={d.ok} id={d.draft_id} "
            f"attachment=Manual — staff to attach PDF"
        )

    staff_subject = (
        f"[Whisker Waggin' MVC] Nonprofit AR Invoice Drafts — {month_name} "
        f"{year} (TEST MODE)"
    )
    staff_body = build_staff_html(
        month_name=month_name,
        weekday=weekday,
        forced=bool(args.force),
        drafts=drafts,
        skipped=skipped,
        signature=signature,
    )
    staff = send_staff_report(composio, subject=staff_subject, body_html=staff_body)
    print(
        "STAFF REPORT:",
        "ok=",
        staff.get("successful"),
        "id=",
        (staff.get("data") or {}).get("id"),
        "url=",
        (staff.get("data") or {}).get("display_url"),
    )
    return 0 if staff.get("successful") and all(d.ok for d in drafts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
