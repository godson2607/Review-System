"""Static app config. Everything a user may want to change lives in the database
and is edited from Admin -> Settings (see settings.py)."""
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)


def _secret_key():
    key_file = INSTANCE_DIR / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    return key_file.read_text().strip()


class Config:
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{INSTANCE_DIR / 'review_system.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB upload limit

    UPLOAD_DIR = BASE_DIR / "uploads"
    OUTBOX_DIR = BASE_DIR / "outbox"
    CERT_DIR = BASE_DIR / "certificates"
