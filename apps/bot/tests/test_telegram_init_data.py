import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from misakabot.telegram_init_data import InitDataError, verify_init_data


def signed_init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "username": "admin"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_accepts_valid_signed_data(self) -> None:
        identity = verify_init_data(signed_init_data("123:token", 55), "123:token")
        self.assertEqual(identity.user_id, 55)
        self.assertEqual(identity.username, "admin")

    def test_rejects_tampered_data(self) -> None:
        payload = signed_init_data("123:token", 55).replace("admin", "attacker")
        with self.assertRaises(InitDataError):
            verify_init_data(payload, "123:token")


if __name__ == "__main__":
    unittest.main()
