from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class QboToken(Base):
    __tablename__ = "qbo_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    realm_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    shepherd_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    client_name: Mapped[str] = mapped_column(String(255), index=True, default="Unknown client")
    line_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    product_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(128), index=True, default="Uncategorized")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class QboExpense(Base):
    __tablename__ = "qbo_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_name: Mapped[str] = mapped_column(String(255), index=True)
    account_group: Mapped[str] = mapped_column(String(128), default="Expenses")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ShepherdReportSnapshot(Base):
    __tablename__ = "shepherd_report_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    service_sales: Mapped[float] = mapped_column(Float, default=0.0)
    inventory_sales: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    gross_margin: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="api")
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ShepherdReportCategory(Base):
    __tablename__ = "shepherd_report_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    category: Mapped[str] = mapped_column(String(255), index=True)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _migrate_schema() -> None:
    with engine.begin() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(invoice_lines)")).fetchall()
        }
        if columns and "client_id" not in columns:
            connection.execute(
                text("ALTER TABLE invoice_lines ADD COLUMN client_id VARCHAR(64) DEFAULT ''")
            )
        if columns and "client_name" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE invoice_lines ADD COLUMN client_name "
                    "VARCHAR(255) DEFAULT 'Unknown client'"
                )
            )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
