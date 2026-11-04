import msal
from app.config import get_settings


def build_msal_app() -> msal.ConfidentialClientApplication:
    settings = get_settings()
    return msal.ConfidentialClientApplication(
        settings.graph_client_id,
        authority=settings.graph_authority,
        client_credential=settings.graph_client_secret,
    ) 