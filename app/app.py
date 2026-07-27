from fastapi import FastAPI

from app.db.models import init_db
from app.main import router

app = FastAPI(title="Vet Margin Dashboard", version="0.1.0")
app.include_router(router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
