from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import InvoiceLine, QboToken, SyncRun
from app.integrations.quickbooks import fetch_profit_and_loss, store_qbo_expenses
from app.integrations.shepherd import ShepherdClient, extract_invoice_lines
from app.integrations.shepherd_report import parse_report_api_payload
from app.services.shepherd_report_store import store_shepherd_report


async def sync_shepherd(db: Session, start_date: datetime, end_date: datetime) -> SyncRun:
    run = SyncRun(source="shepherd", status="running", message="Sync started")
    db.add(run)
    db.commit()

    try:
        client = ShepherdClient()
        product_costs = await client.get_product_costs()
        category_names = await client.get_product_categories()
        journal_cogs = await client.get_inventory_cogs_by_invoice_item(start_date, end_date)
        invoices = await client.get_invoices(start_date, end_date)
        client_ids = [str(invoice.get("clientId") or "") for invoice in invoices if invoice.get("clientId")]
        client_lookup = await client.get_clients(client_ids)
        invoice_ids = [str(invoice["id"]) for invoice in invoices if invoice.get("id")]
        invoice_items = await client.get_invoice_items(invoice_ids=invoice_ids)
        lines = extract_invoice_lines(
            invoices,
            invoice_items,
            product_costs=product_costs,
            journal_cogs=journal_cogs,
            category_names=category_names,
            client_lookup=client_lookup,
        )

        total_revenue = sum(line["revenue"] for line in lines)
        total_cogs = sum(line["cogs"] for line in lines)

        db.query(InvoiceLine).filter(
            InvoiceLine.line_date >= start_date,
            InvoiceLine.line_date <= end_date,
        ).delete(synchronize_session=False)

        incoming_ids = [line["shepherd_id"] for line in lines if line.get("shepherd_id")]
        for index in range(0, len(incoming_ids), 500):
            chunk = incoming_ids[index : index + 500]
            db.query(InvoiceLine).filter(InvoiceLine.shepherd_id.in_(chunk)).delete(
                synchronize_session=False
            )

        for line in lines:
            db.add(InvoiceLine(**line))

        report_note = ""
        try:
            api_payload, report_status = await client.get_revenue_cost_report(start_date, end_date)
            report_data = parse_report_api_payload(api_payload) if api_payload else None
            if report_data:
                store_shepherd_report(db, start_date, end_date, report_data)
                report_note = (
                    f" Shepherd report synced: revenue ${report_data.revenue:,.2f}, "
                    f"COGS ${report_data.cogs:,.2f}."
                )
            elif report_status == 403:
                report_note = (
                    " Revenue & Cost report API returned 403 after using the corrected "
                    "request format — Shepherd support may need to review endpoint authorization."
                )
        except Exception:
            pass

        run.status = "success"
        run.message = (
            f"Synced {len(lines)} invoice lines from {len(invoices)} invoices. "
            f"Revenue ${total_revenue:,.2f}, COGS ${total_cogs:,.2f} "
            f"({len(journal_cogs)} inventory journal entries)."
            f"{report_note}"
        )
        if total_cogs < total_revenue * 0.15 and not report_note:
            run.message += (
                " Note: COGS is a best-effort estimate from inventory data and may be "
                "lower than Shepherd's Revenue & Cost report until that API is enabled."
            )
        if not lines and invoices:
            run.message = (
                f"Fetched {len(invoices)} invoices but no line items were found. "
                "Check invoice item filters for the selected period."
            )
    except Exception as exc:
        run.status = "error"
        run.message = str(exc)

    db.commit()
    db.refresh(run)
    return run


async def sync_qbo(db: Session, start_date: datetime, end_date: datetime) -> SyncRun:
    run = SyncRun(source="qbo", status="running", message="Sync started")
    db.add(run)
    db.commit()

    token = db.query(QboToken).order_by(QboToken.updated_at.desc()).first()
    if token is None:
        run.status = "error"
        run.message = "QuickBooks is not connected. Visit /qbo/connect first."
        db.commit()
        db.refresh(run)
        return run

    try:
        expenses = await fetch_profit_and_loss(token, db, start_date, end_date)
        count = store_qbo_expenses(db, expenses, start_date, end_date)
        total_overhead = sum(expense["amount"] for expense in expenses)
        run.status = "success"
        run.message = f"Synced {count} overhead accounts totaling ${total_overhead:,.2f} from QuickBooks P&L"
    except Exception as exc:
        run.status = "error"
        run.message = str(exc)

    db.commit()
    db.refresh(run)
    return run


async def sync_all(db: Session, start_date: datetime, end_date: datetime) -> SyncRun:
    shepherd_run = await sync_shepherd(db, start_date, end_date)
    qbo_run = await sync_qbo(db, start_date, end_date)

    status = "success" if shepherd_run.status == "success" and qbo_run.status == "success" else "error"
    message = f"Shepherd: {shepherd_run.message} | QuickBooks: {qbo_run.message}"

    run = SyncRun(source="all", status=status, message=message)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
