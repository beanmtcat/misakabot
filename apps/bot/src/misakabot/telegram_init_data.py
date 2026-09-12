from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    username: str | None


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: int = 600) -> TelegramIdentity:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = values.pop("hash", None)
    if not provided_hash:
        raise InitDataError("Missing Telegram initData hash")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_hash, expected_hash):
        raise InitDataError("Invalid Telegram initData signature")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InitDataError("Malformed Telegram initData") from error
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > max_age_seconds:
        raise InitDataError("Expired Telegram initData")
    return TelegramIdentity(user_id=user_id, username=user.get("username"))
