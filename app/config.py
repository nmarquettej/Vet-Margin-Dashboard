import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    shepherd_public_key: str = os.getenv("SHEPHERD_PUBLIC_KEY", "")
    shepherd_private_key: str = os.getenv("SHEPHERD_PRIVATE_KEY", "")
    shepherd_clinic_id: str = os.getenv("SHEPHERD_CLINIC_ID", "")
    shepherd_base_url: str = os.getenv("SHEPHERD_BASE_URL", "https://open-api.shepherd.vet/pav2")
    shepherd_invoice_endpoint: str = os.getenv("SHEPHERD_INVOICE_ENDPOINT", "")

    qbo_client_id: str = os.getenv("QBO_CLIENT_ID", "")
    qbo_client_secret: str = os.getenv("QBO_CLIENT_SECRET", "")
    qbo_redirect_uri: str = os.getenv("QBO_REDIRECT_URI", "http://localhost:8000/qbo/callback")
    qbo_environment: str = os.getenv("QBO_ENVIRONMENT", "sandbox")

    app_secret_key: str = os.getenv("APP_SECRET_KEY", "dev-secret-change-me")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./vet_margin.db")

    @property
    def qbo_api_base(self) -> str:
        if self.qbo_environment == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"

    @property
    def shepherd_configured(self) -> bool:
        return bool(self.shepherd_public_key and self.shepherd_private_key and self.shepherd_clinic_id)

    @property
    def qbo_configured(self) -> bool:
        return bool(self.qbo_client_id and self.qbo_client_secret)


settings = Settings()
