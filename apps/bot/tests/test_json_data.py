import unittest

from misakabot.json_data import json_object


class JsonDataTests(unittest.TestCase):
    def test_nested_json_values_keep_native_types(self) -> None:
        self.assertEqual(
            json_object('{"id":42,"ok":true,"name":"猫猫","value":null,"nested":{"items":[1,"a"]}}'),
            {"id": 42, "ok": True, "name": "猫猫", "value": None, "nested": {"items": [1, "a"]}},
        )

    def test_non_object_and_invalid_json_are_rejected(self) -> None:
        for value in ('[]', 'null', '42', '"text"', '{"invalid"', b'\xff'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                json_object(value)
