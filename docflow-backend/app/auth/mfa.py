"""TOTP-based MFA primitives (pyotp).

Scaffolded per Phase 3 but only enforced per-user via User.mfa_enabled —
off by default, so nothing here changes existing login behavior until a
user explicitly opts in through POST /v1/auth/mfa/setup + /mfa/verify.
"""

import pyotp

ISSUER_NAME = "DocFlow.ai"


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def verify_totp_code(secret: str, code: str) -> bool:
    # valid_window=1 tolerates one 30s step of clock drift either side.
    return bool(pyotp.totp.TOTP(secret).verify(code, valid_window=1))
