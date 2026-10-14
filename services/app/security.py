import base64
import os
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def make_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=settings.jwt_ttl_hours)},
                      settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> str:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"]


def _key() -> bytes:
    if not settings.master_key:
        raise RuntimeError("MASTER_KEY not configured")
    return base64.b64decode(settings.master_key)


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return AESGCM(_key()).encrypt(nonce, plaintext.encode(), None), nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> str:
    return AESGCM(_key()).decrypt(bytes(nonce), bytes(ciphertext), None).decode()
