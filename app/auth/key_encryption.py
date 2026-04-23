"""Symmetric encryption of raw API keys so admins can reveal them later.

We derive a Fernet key deterministically from `settings.secret_key` so the
encryption key rotates with the application secret. If the secret rotates
without migration, existing encrypted_key values become unreadable — but
the underlying hashed key continues to authenticate normally, so requests
keep working; only the "reveal" path degrades to "(not retrievable)".
"""
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    # Fernet requires a URL-safe base64 32-byte key; derive it via SHA-256
    digest = hashlib.sha256(f"llm-proxy-v2:apikey:{settings.secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(raw_key: str) -> str:
    return _fernet().encrypt(raw_key.encode()).decode()


def decrypt_key(encrypted: Optional[str]) -> Optional[str]:
    """Decrypt; return None if ciphertext is missing or unreadable."""
    if not encrypted:
        return None
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError):
        return None
