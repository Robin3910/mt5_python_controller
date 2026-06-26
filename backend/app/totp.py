"""TOTP 双因素认证（兼容 Google Authenticator 等验证器 App）。"""
import base64
import io

import pyotp
import qrcode

ISSUER = "MT5 Hub"


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(username: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def totp_qr_data_uri(provisioning_uri: str) -> str:
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def verify_totp_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)

