"""注册和登录用的密码处理。

这里用 SHA-256 加盐，是为了让初学者看懂「密码不能明文保存」。
正式项目请换成 bcrypt 或 argon2。
"""

import hashlib
import secrets


def new_salt() -> str:
    return secrets.token_hex(8)


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def verify_password(password: str, salt: str, hashed: str) -> bool:
    return secrets.compare_digest(hash_password(password, salt), hashed)


def new_token() -> str:
    return secrets.token_hex(16)
