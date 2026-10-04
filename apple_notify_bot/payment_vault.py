from __future__ import annotations

import json
import re
from dataclasses import dataclass

SERVICE_NAME = "IphoneBot-v8.1"


class PaymentVaultUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentSummary:
    last4: str
    label: str


def _normalize_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _username(profile_id: int) -> str:
    return f"profile:{int(profile_id)}"


def _load_keyring():
    try:
        import keyring
        from keyring.errors import KeyringError, NoKeyringError
    except ImportError as error:
        raise PaymentVaultUnavailable(
            "Secure payment storage is unavailable. Run: pip install -r requirements.txt"
        ) from error
    return keyring, KeyringError, NoKeyringError


def save_payment(
    profile_id: int,
    *,
    card_number: str,
    exp_month: str,
    exp_year: str,
    cvv: str,
    name_on_card: str = "",
) -> PaymentSummary:
    number = _normalize_digits(card_number)
    month = _normalize_digits(exp_month)
    year = _normalize_digits(exp_year)
    security = _normalize_digits(cvv)

    if not 12 <= len(number) <= 19:
        raise ValueError("Card number must contain 12-19 digits")
    if not month or not 1 <= int(month) <= 12:
        raise ValueError("Expiration month must be 1-12")
    if len(year) == 2:
        year = f"20{year}"
    if len(year) != 4:
        raise ValueError("Expiration year must be 2 or 4 digits")
    if len(security) not in (3, 4):
        raise ValueError("CVV must contain 3 or 4 digits")

    payload = {
        "card_number": number,
        "exp_month": month.zfill(2),
        "exp_year": year,
        "cvv": security,
        "name_on_card": (name_on_card or "").strip(),
    }

    keyring, KeyringError, NoKeyringError = _load_keyring()
    try:
        keyring.set_password(SERVICE_NAME, _username(profile_id), json.dumps(payload))
    except (KeyringError, NoKeyringError, RuntimeError) as error:
        raise PaymentVaultUnavailable(
            "Could not store payment securely in the OS credential vault. "
            "On Windows, make sure Windows Credential Manager is available and rerun the app normally (not as a restricted service account)."
        ) from error

    return PaymentSummary(last4=number[-4:], label=f"Card •••• {number[-4:]}")


def get_payment(profile_id: int) -> dict | None:
    keyring, KeyringError, NoKeyringError = _load_keyring()
    try:
        raw = keyring.get_password(SERVICE_NAME, _username(profile_id))
    except (KeyringError, NoKeyringError, RuntimeError) as error:
        raise PaymentVaultUnavailable(
            "Could not read payment from the OS credential vault."
        ) from error
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PaymentVaultUnavailable("Stored payment credential is unreadable") from error
    return value if isinstance(value, dict) else None


def delete_payment(profile_id: int) -> None:
    keyring, KeyringError, NoKeyringError = _load_keyring()
    try:
        keyring.delete_password(SERVICE_NAME, _username(profile_id))
    except keyring.errors.PasswordDeleteError:
        return
    except (KeyringError, NoKeyringError, RuntimeError) as error:
        raise PaymentVaultUnavailable(
            "Could not remove payment from the OS credential vault."
        ) from error
