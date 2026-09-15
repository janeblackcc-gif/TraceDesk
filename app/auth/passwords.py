from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError

# OWASP Argon2id minimum, pinned and explicit; tune upward with deployment evidence.
HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
DUMMY_HASH = HASHER.hash('dummy-verification-no-account-credential')


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128 or '\x00' in password or len(set(password)) < 4:
        raise ValueError('PASSWORD_POLICY')
    return HASHER.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    if len(password) > 128:
        return False
    try:
        valid = HASHER.verify(encoded or DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(valid and encoded is not None)
