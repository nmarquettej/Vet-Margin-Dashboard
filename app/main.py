from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from app.dates import default_period, parse_end_date, parse_start_date
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from urllib.parse import parse_qs, quote, urlparse

from app.config import settings
from app.db.models import InvoiceLine, QboToken, SyncRun, get_db

from app.integrations.quickbooks import (
    disconnect_qbo,
    exchange_code_for_tokens,
    fetch_company_name,
    get_authorization_url,
    intuit_logout_url,
)
from app.services.margins import build_practice_summary
from app.services.sync import sync_all, sync_qbo, sync_shepherd

templates = Jinja2Templates(directory="app/templates")


def _format_currency(value: float | int | None) -> str:
    return f"${float(value or 0):,.2f}"


templates.env.filters["currency"] = _format_currency
router = APIRouter()


def _parse_float(value: str) -> float:
    cleaned = (value or "").strip().replace("$", "").replace(",", "")
    if not cleaned:
        return 0.0
    return float(cleaned)


def _pick(row: dict[str, str], *names: str) -> str:
    lowered = {key.lower().strip(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return ""


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    default_start, default_end = default_period()
    start_date = parse_start_date(start) if start else default_start
    end_date = parse_end_date(end) if end else default_end

    summary = build_practice_summary(db, start_date, end_date)
    qbo_token = db.query(QboToken).order_by(QboToken.updated_at.desc()).first()
    qbo_connected = qbo_token is not None
    qbo_company_name = ""
    if qbo_token is not None:
        try:
            qbo_company_name = await fetch_company_name(qbo_token, db)
        except Exception:
            qbo_company_name = ""
    recent_syncs = db.query(SyncRun).order_by(SyncRun.created_at.desc()).limit(8).all()
    qbo_is_sandbox = settings.qbo_environment != "production"
    qbo_status = request.query_params.get("qbo", "")
    qbo_error_detail = request.query_params.get("detail", "")

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "summary": summary,
            "start_date": start_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "shepherd_configured": settings.shepherd_configured,
            "qbo_configured": settings.qbo_configured,
            "qbo_connected": qbo_connected,
            "qbo_environment": settings.qbo_environment,
            "qbo_is_sandbox": qbo_is_sandbox,
            "qbo_company_name": qbo_company_name,
            "qbo_redirect_uri": settings.qbo_redirect_uri,
            "qbo_status": qbo_status,
            "qbo_error_detail": qbo_error_detail,
            "recent_syncs": recent_syncs,
        },
    )


@router.get("/qbo/connect")
async def qbo_connect(
    request: Request,
    db: Session = Depends(get_db),
    fresh: int = Query(default=1),
    logout: int = Query(default=0),
):
    if not settings.qbo_configured:
        return RedirectResponse("/", status_code=303)

    # Always clear any prior connection before starting OAuth.
    disconnect_qbo(db)
    state = f"vet-margin-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    authorize_url = get_authorization_url(state=state, force_login=True)

    # Optional Intuit logout hop. Default OFF because Intuit often ignores
    # redirect_uri and drops the user on their account home (breaking OAuth).
    if logout:
        host = str(request.base_url).rstrip("/")
        return_to = f"{host}/qbo/authorize?state={quote(state)}"
        return RedirectResponse(intuit_logout_url(return_to), status_code=303)

    return RedirectResponse(authorize_url, status_code=303)


@router.get("/qbo/authorize")
async def qbo_authorize(state: str = Query(default="vet-margin-dashboard")):
    """Second hop after Intuit logout — start OAuth with a fresh login prompt."""
    if not settings.qbo_configured:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(
        get_authorization_url(state=state, force_login=True),
        status_code=303,
    )


@router.get("/qbo/disconnect")
async def qbo_disconnect(db: Session = Depends(get_db)):
    disconnect_qbo(db)
    return RedirectResponse("/?qbo=disconnected", status_code=303)


@router.get("/qbo/reset")
async def qbo_reset(db: Session = Depends(get_db)):
    """Hard reset connection state, then send the user to a fresh Intuit login."""
    disconnect_qbo(db)
    return RedirectResponse("/qbo/connect?fresh=1", status_code=303)


def _extract_oauth_callback(raw: str) -> tuple[str, str]:
    """Parse code + realmId from a pasted Intuit/GitHub Pages callback URL."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Paste the full callback URL from the OAuth catcher page.")
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    if not query and "code=" in text:
        # Allow pasting a raw query string.
        query = parse_qs(text.lstrip("?"))
    code = (query.get("code") or [""])[0].strip()
    realm_id = (query.get("realmId") or query.get("realm_id") or [""])[0].strip()
    if not code or not realm_id:
        raise ValueError("Callback URL must include code and realmId query parameters.")
    return code, realm_id


@router.get("/qbo/callback")
async def qbo_callback(
    code: str = Query(...),
    realmId: str = Query(...),
    db: Session = Depends(get_db),
):
    # Ensure only the newly authorized company remains.
    disconnect_qbo(db)
    exchange_code_for_tokens(code, realmId, db)
    return RedirectResponse("/?qbo=connected", status_code=303)


@router.post("/qbo/finish")
async def qbo_finish(
    db: Session = Depends(get_db),
    callback_url: str = Form(...),
):
    """Finish OAuth when Intuit redirected to the permanent GitHub Pages catcher."""
    try:
        code, realm_id = _extract_oauth_callback(callback_url)
    except ValueError as exc:
        return RedirectResponse(f"/?qbo=error&detail={quote(str(exc))}", status_code=303)
    disconnect_qbo(db)
    try:
        exchange_code_for_tokens(code, realm_id, db)
    except Exception as exc:
        return RedirectResponse(
            f"/?qbo=error&detail={quote(f'Token exchange failed: {exc}')}",
            status_code=303,
        )
    return RedirectResponse("/?qbo=connected", status_code=303)


@router.post("/sync/shepherd")
async def trigger_shepherd_sync(
    db: Session = Depends(get_db),
    start: str = Query(...),
    end: str = Query(...),
):
    run = await sync_shepherd(db, parse_start_date(start), parse_end_date(end))
    return {"status": run.status, "message": run.message}


@router.post("/sync/all")
async def trigger_sync_all(
    db: Session = Depends(get_db),
    start: str = Query(...),
    end: str = Query(...),
):
    run = await sync_all(db, parse_start_date(start), parse_end_date(end))
    return {"status": run.status, "message": run.message}


@router.post("/sync/qbo")
async def trigger_qbo_sync(
    db: Session = Depends(get_db),
    start: str = Query(...),
    end: str = Query(...),
):
    run = await sync_qbo(db, parse_start_date(start), parse_end_date(end))
    return {"status": run.status, "message": run.message}


@router.post("/import/shepherd-csv")
async def import_shepherd_csv(
    db: Session = Depends(get_db),
    start: str = Query(...),
    end: str = Query(...),
    file: UploadFile = File(...),
):
    start_date = parse_start_date(start)
    end_date = parse_end_date(end)
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    db.query(InvoiceLine).filter(
        InvoiceLine.line_date >= start_date,
        InvoiceLine.line_date <= end_date,
    ).delete()

    imported = 0
    for index, row in enumerate(reader, start=1):
        revenue = _parse_float(_pick(row, "revenue", "total", "amount", "price"))
        cogs = _parse_float(_pick(row, "cogs", "cost", "total cost", "product cost"))
        if revenue == 0 and cogs == 0:
            continue

        line_date_raw = _pick(row, "date", "invoice date", "line date", "date created")
        line_date = datetime.fromisoformat(line_date_raw) if line_date_raw else start_date
        product_name = _pick(row, "product", "product name", "item", "description", "service") or "Imported item"
        category = _pick(row, "category", "product category", "service category") or "Imported"

        db.add(
            InvoiceLine(
                shepherd_id=f"csv:{index}:{product_name}:{revenue}",
                invoice_id=f"csv-import:{start_date.date()}:{end_date.date()}",
                line_date=line_date,
                product_name=product_name,
                category=category,
                quantity=1.0,
                revenue=revenue,
                cogs=cogs,
            )
        )
        imported += 1

    run = SyncRun(
        source="shepherd-csv",
        status="success" if imported else "error",
        message=f"Imported {imported} rows from {file.filename}" if imported else "No usable rows found in CSV",
    )
    db.add(run)
    db.commit()
    return {"status": run.status, "message": run.message}


@router.get("/export/summary.csv")
async def export_summary_csv(
    db: Session = Depends(get_db),
    start: str = Query(...),
    end: str = Query(...),
):
    summary = build_practice_summary(db, parse_start_date(start), parse_end_date(end))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Vet Margin Dashboard Summary"])
    writer.writerow(["Period Start", summary.period_start.date().isoformat()])
    writer.writerow(["Period End", summary.period_end.date().isoformat()])
    writer.writerow([])
    writer.writerow(["Metric", "Amount"])
    writer.writerow(["Revenue", f"{summary.revenue:.2f}"])
    writer.writerow(["COGS", f"{summary.cogs:.2f}"])
    writer.writerow(["Gross Margin", f"{summary.gross_margin:.2f}"])
    writer.writerow(["Overhead", f"{summary.overhead:.2f}"])
    writer.writerow(["Net Margin", f"{summary.net_margin:.2f}"])
    writer.writerow([])
    writer.writerow(["Category", "Revenue", "COGS", "Gross Margin", "Allocated Overhead", "Net Margin"])
    for category in summary.categories:
        writer.writerow(
            [
                category.category,
                f"{category.revenue:.2f}",
                f"{category.cogs:.2f}",
                f"{category.gross_margin:.2f}",
                f"{category.allocated_overhead:.2f}",
                f"{category.net_margin:.2f}",
            ]
        )

    output.seek(0)
    filename = f"vet-margin-{summary.period_start.date()}-to-{summary.period_end.date()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
