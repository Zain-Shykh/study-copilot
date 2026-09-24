"""Loads configuration and secrets from the local .env file."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import dotenv

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

# Single-user app — the user is in Pakistan. Used wherever "today"/"yesterday"
# or a calendar-day boundary needs to match the user's actual local day
# rather than UTC.
USER_TIMEZONE = ZoneInfo("Asia/Karachi")

_REQUIRED_ENV_VARS = [
    "DATABASE_URL",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "META_WHATSAPP_ACCESS_TOKEN",
    "META_WHATSAPP_PHONE_NUMBER_ID",
    "META_WEBHOOK_VERIFY_TOKEN",
    "META_APP_SECRET",
    "MY_WHATSAPP_NUMBER",
    "GEMINI_API_KEY",
]


@dataclass
class Settings:
    database_url: str
    google_oauth_client_id: str
    google_oauth_client_secret: str
    meta_whatsapp_access_token: str
    meta_whatsapp_phone_number_id: str
    meta_webhook_verify_token: str
    meta_app_secret: str
    my_whatsapp_number: str
    gemini_api_key: str
    gemini_model: str
    student_info: str


def load_settings() -> Settings:
    """Loads .env and reads each required field from the environment.

    Raises RuntimeError listing every missing required var at once if any
    are unset, so misconfiguration fails at startup rather than on first use.
    """
    dotenv.load_dotenv()

    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required env var(s): {', '.join(missing)}")

    return Settings(
        database_url=os.environ["DATABASE_URL"],
        google_oauth_client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        google_oauth_client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        meta_whatsapp_access_token=os.environ["META_WHATSAPP_ACCESS_TOKEN"],
        meta_whatsapp_phone_number_id=os.environ["META_WHATSAPP_PHONE_NUMBER_ID"],
        meta_webhook_verify_token=os.environ["META_WEBHOOK_VERIFY_TOKEN"],
        meta_app_secret=os.environ["META_APP_SECRET"],
        my_whatsapp_number=os.environ["MY_WHATSAPP_NUMBER"],
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        gemini_model=os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        student_info=os.environ.get("STUDENT_INFO", ""),
    )
