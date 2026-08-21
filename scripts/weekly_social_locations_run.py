#!/usr/bin/env python3
"""Whisker Waggin weekly social-media location planner.

Pulls this week's (Mon–Sun, America/New_York) Shepherd appointments, groups them
by inferred service area, highlights community / rescue-aligned photo stops and
nearby events, and emails a staff planning report.

Modes:
  --extract-only  Print/write schedule JSON only (no Gmail)
  --live          Extract, research (Composio Search), email staff report
  --dry-run       Extract + research + write HTML report file; do not send

Note: Shepherd OpenAPI does not expose client street addresses. Location signals
come from visit-reason hints, known partner orgs, and optional Google Calendar
event locations when a Calendar connection is available via Composio.
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
STAFF_CC = ["nmarquettej@gmail.com"]
FAILURE_TO = "nmarquettej@gmail.com"
GMAIL_TOOLKIT_VERSION = "20260721_00"

TERMINAL_STATUSES = {
    "canceled",
    "cancelled",
    "no show",
    "noshow",
}

# Public / partner locations we can safely publish for social planning.
KNOWN_ORGS: dict[str, dict[str, str]] = {
    "feline urgent rescue": {
        "label": "Feline Urgent Rescue of WNC (FUR)",
        "address": "38 Safe Haven Drive, Waynesville, NC 28786",
        "area": "Waynesville / Haywood County",
        "brand_fit": "Rescue / 501(c)(3) partner — strong community + rescue story",
        "url": "https://furofwnc.org/",
    },
    "fur": {
        "label": "Feline Urgent Rescue of WNC (FUR)",
        "address": "38 Safe Haven Drive, Waynesville, NC 28786",
        "area": "Waynesville / Haywood County",
        "brand_fit": "Rescue / 501(c)(3) partner — strong community + rescue story",
        "url": "https://furofwnc.org/",
    },
    "house cat museum": {
        "label": "American Museum of the House Cat",
        "address": "5063 US Highway 441 South, Sylva, NC 28779",
        "area": "Sylva / Jackson County",
        "brand_fit": "Beloved local landmark; cat-culture photo gold; family + community",
        "url": "https://www.wnccatmuseum.org/",
    },
}

# Regex → area inference from visitReason / client name when street address is unavailable.
AREA_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bswain\b|\bbryson\b", re.I), "Bryson City / Swain County"),
    (re.compile(r"\bsylva\b|\bcullowhee\b|\bjackson\b|house\s*cat\s*museum", re.I), "Sylva / Jackson County"),
    (re.compile(r"\bfur\b|feline\s+urgent|\bwaynesville\b|\bhaywood\b|\bfrog\s*level\b", re.I), "Waynesville / Haywood County"),
    (re.compile(r"\basheville\b|\bavl\b|\bweaverville\b|\barden\b", re.I), "Asheville area"),
]

# Curated photo / stop playbook keyed by area (brand: community, family, rescue-friendly).
AREA_PLAYBOOK: dict[str, list[dict[str, str]]] = {
    "Waynesville / Haywood County": [
        {
            "name": "Welcome to Frog Level Mural",
            "address": "14 Commerce St, Waynesville, NC 28786",
            "why": "Iconic Frog Level mural — local pride, colorful van/team selfie backdrop",
            "type": "photo",
        },
        {
            "name": "Gateway to the Great Smoky Mountains Arch",
            "address": "41 S Main St, Waynesville, NC 28786",
            "why": "Downtown landmark that reads as “home base / mountain community”",
            "type": "photo",
        },
        {
            "name": "FUR of WNC",
            "address": "38 Safe Haven Drive, Waynesville, NC 28786",
            "why": "Rescue partner content — mission-aligned, 501(c)(3) friendly",
            "type": "rescue",
        },
        {
            "name": "Haywood's Historic Farmers Market (Sat)",
            "address": "HART Theater Parking Lot, 250 Pigeon St, Waynesville, NC",
            "why": "Weekly community touchpoint when the route is in Haywood on Saturdays",
            "type": "event",
        },
    ],
    "Sylva / Jackson County": [
        {
            "name": "American Museum of the House Cat",
            "address": "5063 US-441 S, Sylva, NC 28779",
            "why": "One-of-a-kind WNC attraction; sentimental + shareable cat content",
            "type": "photo",
        },
        {
            "name": "Downtown Sylva murals / Main Street",
            "address": "W Main St, Sylva, NC 28779",
            "why": "Walkable mural + small-town Main Street shots between appointments",
            "type": "photo",
        },
        {
            "name": "Bridge Park",
            "address": "76 Railroad Ave, Sylva, NC 28779",
            "why": "Quick outdoor atmosphere shot with local character",
            "type": "photo",
        },
    ],
    "Bryson City / Swain County": [
        {
            "name": "Swain County Wall of History",
            "address": "101 Mitchell St, Bryson City, NC 28713",
            "why": "Sentimental local history sculpture wall — community storytelling",
            "type": "photo",
        },
        {
            "name": "Jeff Marley's Freestone Trout",
            "address": "117 Island St, Bryson City, NC 28713",
            "why": "Beloved public sculpture; strong “we’re in your town” visual",
            "type": "photo",
        },
        {
            "name": "Swain County Visitor Center & Heritage Museum",
            "address": "2 Everett St, Bryson City, NC 28713",
            "why": "Heritage backdrop + visitor-friendly stop near downtown",
            "type": "photo",
        },
    ],
    "Asheville area": [
        {
            "name": "Local rescue / adoption pop-ups (check calendar)",
            "address": "Asheville metro",
            "why": "Pair route days with Mountain Pet Rescue / humane-society events when timing aligns",
            "type": "event",
        },
    ],
}


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


def week_bounds(anchor: datetime | None = None) -> tuple[datetime, datetime]:
    """Monday 00:00 → next Monday 00:00 in America/New_York."""
    local = (anchor or datetime.now(TZ)).astimezone(TZ)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7)


def client_display_name(client: dict[str, Any] | None) -> str:
    if not isinstance(client, dict):
        return "Unknown"
    first = str(client.get("firstName") or "").strip()
    last = str(client.get("lastName") or "").strip()
    name = " ".join(p for p in (first, last) if p).strip()
    if name:
        return name
    return str(client.get("name") or client.get("displayName") or "Unknown").strip() or "Unknown"


def infer_area(*texts: str | None) -> str | None:
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return None
    for pattern, area in AREA_HINTS:
        if pattern.search(blob):
            return area
    return None


def match_known_org(*texts: str | None) -> dict[str, str] | None:
    blob = " ".join(t for t in texts if t).lower()
    # Prefer longer keys first so "feline urgent rescue" wins over "fur".
    for key, org in sorted(KNOWN_ORGS.items(), key=lambda kv: -len(kv[0])):
        if " " in key:
            if key in blob:
                return org
            continue
        # Short tokens (e.g. "fur") must be whole words — avoid matching inside FVRCP.
        if re.search(rf"\b{re.escape(key)}\b", blob):
            return org
    return None


def format_appt_local(start: datetime) -> str:
    local = start.astimezone(TZ)
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{local.strftime('%a')} {local.month}/{local.day} {hour}:{local.strftime('%M%p').lower()}"


def extract_week(week_start: datetime | None = None) -> dict[str, Any]:
    monday, sunday = week_bounds(week_start)
    date_from = monday.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    appointments = fetch_paginated(
        "open-api-appointments",
        {
            "dateFrom": date_from,
            "embed": "appointmentStatus,appointmentType",
            "isDeleted": False,
        },
    )

    week_rows: list[dict[str, Any]] = []
    client_ids: list[str] = []
    for appt in appointments:
        start = parse_dt(appt.get("startDate"))
        if not start:
            continue
        local = start.astimezone(TZ)
        if not (monday <= local < sunday):
            continue
        status_obj = appt.get("appointmentStatus") if isinstance(appt.get("appointmentStatus"), dict) else {}
        status = str(status_obj.get("name") or "")
        if status.lower() in TERMINAL_STATUSES:
            continue
        cid = str(appt.get("clientId") or "") or None
        if cid:
            client_ids.append(cid)
        type_obj = appt.get("appointmentType") if isinstance(appt.get("appointmentType"), dict) else {}
        week_rows.append(
            {
                "appointment_id": appt.get("id"),
                "start": start.isoformat(),
                "display": format_appt_local(start),
                "day_key": local.strftime("%Y-%m-%d"),
                "day_label": local.strftime("%A %-m/%-d") if False else local.strftime("%A %m/%d").lstrip("0").replace(" 0", " "),
                "status": status,
                "client_id": cid,
                "visit_reason": appt.get("visitReason") or "",
                "appointment_type": type_obj.get("name") if type_obj else None,
            }
        )

    # Normalize day_label without %-d portability issues
    for row in week_rows:
        local = parse_dt(row["start"])
        assert local is not None
        local = local.astimezone(TZ)
        row["day_label"] = f"{local.strftime('%A')} {local.month}/{local.day}"

    clients: dict[str, dict[str, Any]] = {}
    unique_ids = list(dict.fromkeys(client_ids))
    for index in range(0, len(unique_ids), 200):
        chunk = unique_ids[index : index + 200]
        for client in fetch_paginated("open-api-clients", {"ids": ",".join(chunk)}):
            clients[str(client["id"])] = client

    enriched: list[dict[str, Any]] = []
    areas_seen: set[str] = set()
    for row in sorted(week_rows, key=lambda r: r["start"]):
        client = clients.get(row["client_id"] or "") if row["client_id"] else None
        name = client_display_name(client) if client else "(schedule block / no client)"
        org = match_known_org(name, row["visit_reason"])
        area = (org or {}).get("area") or infer_area(name, row["visit_reason"])
        if area:
            areas_seen.add(area)
        address = (org or {}).get("address")
        enriched.append(
            {
                **row,
                "client_name": name,
                "area": area,
                "address": address,
                "org_label": (org or {}).get("label"),
                "brand_fit": (org or {}).get("brand_fit"),
                "address_source": "known_org" if address else ("area_hint" if area else "unknown"),
            }
        )

    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in enriched:
        by_day.setdefault(row["day_label"], []).append(row)

    return {
        "week_start": monday.date().isoformat(),
        "week_end_exclusive": sunday.date().isoformat(),
        "timezone": "America/New_York",
        "appointment_count": len(enriched),
        "areas": sorted(areas_seen),
        "appointments": enriched,
        "by_day": by_day,
        "address_note": (
            "Shepherd OpenAPI does not return client street addresses. "
            "Addresses shown are from known partner orgs or area hints in the visit reason. "
            "Connect Google Calendar (event locations) for full street-level routing."
        ),
    }


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


def search_execute(composio, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return composio.tools.execute(slug=slug, arguments=arguments)


def load_signature(composio) -> str:
    result = gmail_execute(composio, "GMAIL_LIST_SEND_AS", {})
    data = result.get("data") or {}
    send_as = data.get("sendAs") or data.get("send_as") or []
    if not send_as:
        raise RuntimeError(f"GMAIL_LIST_SEND_AS returned no sendAs entries: {result!r}")
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


def try_calendar_locations(composio, monday: datetime, sunday: datetime) -> list[dict[str, Any]]:
    """Optional: pull Google Calendar events that include a location string."""
    try:
        result = composio.tools.execute(
            slug="GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
            arguments={
                "time_min": monday.isoformat(),
                "time_max": sunday.isoformat(),
                "single_events": True,
                "show_deleted": False,
                "response_detail": "full",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return [{"_error": f"calendar_unavailable: {exc}"}]

    data = result.get("data") or {}
    if not result.get("successful", True) and result.get("error"):
        return [{"_error": str(result.get("error"))}]

    rows: list[dict[str, Any]] = []
    for item in data.get("summary_view") or []:
        loc = item.get("location") or item.get("loc")
        if not loc:
            continue
        rows.append(
            {
                "title": item.get("title") or item.get("summary"),
                "start": item.get("start"),
                "end": item.get("end"),
                "location": loc,
            }
        )
    for wrapped in data.get("events") or []:
        event = wrapped.get("event") if isinstance(wrapped, dict) else None
        if not isinstance(event, dict):
            continue
        loc = event.get("location")
        if not loc:
            continue
        start = event.get("start") or {}
        rows.append(
            {
                "title": event.get("summary"),
                "start": start.get("dateTime") or start.get("date"),
                "location": loc,
            }
        )
    # Dedupe
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = f"{row.get('start')}|{row.get('location')}|{row.get('title')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def research_weekly_events(composio, areas: list[str], week_label: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    queries = []
    for area in areas or ["Waynesville NC", "Sylva NC"]:
        short = area.split("/")[0].strip()
        queries.append(
            f"community events festivals farmers market rescue adoption {short} North Carolina {week_label}"
        )
    queries.append(
        f"animal rescue adoption events Asheville Waynesville Sylva week of {week_label}"
    )

    for query in queries[:4]:
        try:
            result = search_execute(composio, "COMPOSIO_SEARCH_WEB", {"query": query})
            data = result.get("data") or {}
            answer = data.get("answer") or ""
            citations = data.get("citations") or []
            if answer:
                findings.append(
                    {
                        "query": query,
                        "summary": answer[:1200],
                        "sources": ", ".join(
                            str(c.get("url") or c.get("id") or "") for c in citations[:4] if c
                        ),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            findings.append({"query": query, "summary": f"(search failed: {exc})", "sources": ""})
    return findings


def build_highlights(schedule: dict[str, Any], event_research: list[dict[str, str]]) -> dict[str, Any]:
    areas = schedule.get("areas") or []
    # Always include Waynesville playbook as home-base fallback when no areas inferred
    playbook_areas = list(areas) if areas else ["Waynesville / Haywood County"]
    if "Waynesville / Haywood County" not in playbook_areas:
        # Home base still useful for between-call content
        playbook_areas.append("Waynesville / Haywood County")

    stops: list[dict[str, Any]] = []
    for area in playbook_areas:
        for item in AREA_PLAYBOOK.get(area, []):
            stops.append({"area": area, **item})

    # Partner appointments get pinned highlight
    partner_pins = []
    for appt in schedule.get("appointments") or []:
        if appt.get("org_label") and appt.get("address"):
            partner_pins.append(
                {
                    "when": appt["display"],
                    "name": appt["org_label"],
                    "address": appt["address"],
                    "why": appt.get("brand_fit") or "Partner / landmark stop on this week's route",
                    "area": appt.get("area"),
                }
            )

    return {
        "partner_pins": partner_pins,
        "recommended_stops": stops,
        "event_research": event_research,
        "brand_lens": (
            "Prioritize stops that feel community-oriented, family-owned, and "
            "rescue/501(c)(3)-friendly. Prefer murals, statues, and local landmarks "
            "with sentimental or hometown resonance over generic tourist spots."
        ),
    }


def build_report_html(
    schedule: dict[str, Any],
    highlights: dict[str, Any],
    calendar_rows: list[dict[str, Any]],
    signature_html: str,
    report_date: str,
) -> str:
    def esc(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    week_label = f"{schedule['week_start']} → {schedule['week_end_exclusive']}"

    day_sections: list[str] = []
    for day, rows in schedule.get("by_day", {}).items():
        lines = []
        for r in rows:
            addr = r.get("address") or (f"Area: {r['area']}" if r.get("area") else "Address TBD")
            reason = (r.get("visit_reason") or "").strip()
            reason_bit = f"<br><span style='color:#555;font-size:12px;'>{esc(reason[:180])}</span>" if reason else ""
            lines.append(
                f"<li><strong>{esc(r['display'])}</strong> — {esc(r['client_name'])}"
                f"<br><em>{esc(addr)}</em>{reason_bit}</li>"
            )
        day_sections.append(
            f"<h3 style='margin:14px 0 6px;'>{esc(day)}</h3><ul style='margin-top:0;'>{''.join(lines)}</ul>"
        )

    pin_html = ""
    if highlights.get("partner_pins"):
        items = "".join(
            f"<li><strong>{esc(p['when'])}</strong> — {esc(p['name'])}<br>"
            f"{esc(p['address'])}<br><span style='color:#555;'>{esc(p['why'])}</span></li>"
            for p in highlights["partner_pins"]
        )
        pin_html = f"<h3>Partner / landmark pins this week</h3><ul>{items}</ul>"

    stop_rows = []
    for s in highlights.get("recommended_stops") or []:
        stop_rows.append(
            "<tr>"
            f"<td style='border:1px solid #ccc;padding:6px 10px;vertical-align:top;'>{esc(s.get('area'))}</td>"
            f"<td style='border:1px solid #ccc;padding:6px 10px;vertical-align:top;'>{esc(s.get('name'))}</td>"
            f"<td style='border:1px solid #ccc;padding:6px 10px;vertical-align:top;'>{esc(s.get('type'))}</td>"
            f"<td style='border:1px solid #ccc;padding:6px 10px;vertical-align:top;'>{esc(s.get('address'))}</td>"
            f"<td style='border:1px solid #ccc;padding:6px 10px;vertical-align:top;'>{esc(s.get('why'))}</td>"
            "</tr>"
        )
    stops_table = (
        "<table style='border-collapse:collapse;margin:8px 0;width:100%;'>"
        "<tr>"
        "<th style='border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;'>Area</th>"
        "<th style='border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;'>Stop</th>"
        "<th style='border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;'>Type</th>"
        "<th style='border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;'>Address</th>"
        "<th style='border:1px solid #ccc;padding:6px 10px;background:#f5f5f5;text-align:left;'>Why it fits</th>"
        "</tr>"
        + "".join(stop_rows)
        + "</table>"
    )

    event_bits = []
    for finding in highlights.get("event_research") or []:
        event_bits.append(
            f"<p><strong>Research:</strong> {esc(finding.get('summary'))}<br>"
            f"<span style='font-size:12px;color:#666;'>Sources: {esc(finding.get('sources'))}</span></p>"
        )
    events_html = "".join(event_bits) or "<p><em>No live event research returned for this run.</em></p>"

    cal_html = ""
    cal_errors = [r for r in calendar_rows if r.get("_error")]
    cal_ok = [r for r in calendar_rows if not r.get("_error")]
    if cal_ok:
        items = "".join(
            f"<li>{esc(r.get('start'))} — {esc(r.get('title'))}: <em>{esc(r.get('location'))}</em></li>"
            for r in cal_ok
        )
        cal_html = f"<h3>Google Calendar locations</h3><ul>{items}</ul>"
    elif cal_errors:
        cal_html = (
            "<h3>Google Calendar locations</h3>"
            f"<p><em>{esc(cal_errors[0].get('_error'))}. "
            "Connect Calendar in Composio to merge street addresses from the route calendar.</em></p>"
        )

    areas = ", ".join(schedule.get("areas") or []) or "(no areas inferred yet)"

    return f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45;color:#222;">
<p><strong>[WWMVC] Weekly Social Location Plan — {esc(report_date)}</strong></p>
<p>Week window (America/New_York): <strong>{esc(week_label)}</strong><br>
Active appointments: <strong>{esc(schedule.get('appointment_count'))}</strong> · Areas in play: <strong>{esc(areas)}</strong></p>
<p style="color:#555;">{esc(schedule.get('address_note'))}</p>
<p><em>Brand lens:</em> {esc(highlights.get('brand_lens'))}</p>

<h2 style="margin-bottom:4px;">1) This week's route snapshot</h2>
{''.join(day_sections) if day_sections else '<p><em>No active appointments this week.</em></p>'}

{pin_html}
{cal_html}

<h2 style="margin-bottom:4px;">2) High-interest stops for social (plan to swing by)</h2>
{stops_table}

<h2 style="margin-bottom:4px;">3) Events happening while we're in the area</h2>
{events_html}

<h2 style="margin-bottom:4px;">4) Suggested shoot priorities</h2>
<ol>
<li>Lead with rescue / 501(c)(3) partner moments when the schedule already takes you there.</li>
<li>Grab one mural or statue per town cluster (Frog Level, Sylva Main, Bryson sculptures).</li>
<li>If a community event overlaps a route day, plan a 10–15 minute stop for B-roll + a short caption about showing up for neighbors.</li>
</ol>
</div>
<br>
{signature_html}"""


def send_failure_note(composio, body: str) -> None:
    gmail_execute(
        composio,
        "GMAIL_SEND_EMAIL",
        {
            "recipient_email": FAILURE_TO,
            "subject": "[WWMVC] Weekly social locations run — failure",
            "body": body,
            "is_html": False,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly social-media location planner")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--extract-only", action="store_true", help="Dump schedule JSON only")
    mode.add_argument("--dry-run", action="store_true", help="Build report but do not email")
    mode.add_argument("--live", action="store_true", help="Research + email staff report")
    parser.add_argument(
        "--week-start",
        default=None,
        help="Optional YYYY-MM-DD Monday (or any date in the target week). Default: current week.",
    )
    parser.add_argument("--out", default="/tmp/weekly_social_locations.json")
    parser.add_argument("--html-out", default="/tmp/weekly_social_locations.html")
    parser.add_argument(
        "--highlights-file",
        default=None,
        help="Optional JSON file merging extra agent-researched stops/events",
    )
    args = parser.parse_args()

    if not (args.extract_only or args.dry_run or args.live):
        args.dry_run = True

    anchor = None
    if args.week_start:
        anchor = datetime.fromisoformat(args.week_start).replace(tzinfo=TZ)

    result: dict[str, Any] = {
        "mode": "extract-only" if args.extract_only else ("live" if args.live else "dry-run"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        schedule = extract_week(anchor)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "shepherd_failure"
        result["error"] = str(exc)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 1

    result["schedule"] = schedule

    if args.extract_only:
        result["status"] = "extract-ok"
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 0

    monday, sunday = week_bounds(anchor)
    week_label = f"{monday.strftime('%B')} {monday.day}–{(sunday - timedelta(days=1)).day}, {monday.year}"
    local_now = datetime.now(TZ)
    report_date = f"{local_now.strftime('%B')} {local_now.day}, {local_now.year}"

    event_research: list[dict[str, str]] = []
    calendar_rows: list[dict[str, Any]] = []
    signature_html = "<p></p>"
    composio = None

    try:
        composio = get_composio()
        calendar_rows = try_calendar_locations(composio, monday, sunday)
        event_research = research_weekly_events(composio, schedule.get("areas") or [], week_label)
        if args.live or args.dry_run:
            # Signature only required for live send; dry-run still loads when possible
            try:
                signature_html = load_signature(composio)
                result["signature_chars"] = len(signature_html)
            except Exception as sig_exc:  # noqa: BLE001
                if args.live:
                    raise
                result["signature_warning"] = str(sig_exc)
                signature_html = "<p style='color:#888;'><em>(signature unavailable in dry-run)</em></p>"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "composio_failure"
        result["error"] = str(exc)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps(result, indent=2, default=str))
        return 1

    # Merge calendar areas into schedule areas when locations mention known towns
    for row in calendar_rows:
        if row.get("_error"):
            continue
        loc = str(row.get("location") or "")
        area = infer_area(loc)
        if area and area not in schedule["areas"]:
            schedule["areas"].append(area)

    highlights = build_highlights(schedule, event_research)
    if args.highlights_file and os.path.isfile(args.highlights_file):
        with open(args.highlights_file, encoding="utf-8") as f:
            extra = json.load(f)
        if isinstance(extra, dict):
            for key in ("partner_pins", "recommended_stops", "event_research"):
                if isinstance(extra.get(key), list):
                    highlights.setdefault(key, []).extend(extra[key])

    report_html = build_report_html(schedule, highlights, calendar_rows, signature_html, report_date)
    with open(args.html_out, "w", encoding="utf-8") as f:
        f.write(report_html)

    result["highlights"] = highlights
    result["calendar_locations"] = calendar_rows
    result["html_out"] = args.html_out
    result["report_date"] = report_date
    result["week_label"] = week_label

    if args.dry_run:
        result["status"] = "dry-run-ok"
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(json.dumps({k: result[k] for k in result if k != "highlights"}, indent=2, default=str))
        print(json.dumps({"highlights_preview": {
            "partner_pins": highlights.get("partner_pins"),
            "stops": len(highlights.get("recommended_stops") or []),
            "event_research_count": len(highlights.get("event_research") or []),
        }}, indent=2))
        return 0

    # LIVE send
    assert composio is not None
    subject = f"[WWMVC] Weekly Social Location Plan — week of {schedule['week_start']}"
    try:
        send_result = gmail_execute(
            composio,
            "GMAIL_SEND_EMAIL",
            {
                "recipient_email": STAFF_TO,
                "extra_recipients": STAFF_CC,
                "subject": subject,
                "body": report_html,
                "is_html": True,
            },
        )
        result["staff_report"] = {
            "ok": True,
            "subject": subject,
            "to": STAFF_TO,
            "cc": STAFF_CC,
            "message_id": extract_message_id(send_result),
        }
        result["status"] = "success"
    except Exception as exc:  # noqa: BLE001
        result["staff_report"] = {"ok": False, "error": str(exc), "subject": subject}
        result["status"] = "send_failure"
        try:
            send_failure_note(composio, f"Weekly social locations staff report failed:\n{exc}")
        except Exception as gexc:  # noqa: BLE001
            result["failure_note_error"] = str(gexc)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(json.dumps({
        "status": result["status"],
        "week_start": schedule["week_start"],
        "appointment_count": schedule["appointment_count"],
        "areas": schedule["areas"],
        "staff_report": result.get("staff_report"),
        "html_out": args.html_out,
        "out": args.out,
    }, indent=2, default=str))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
