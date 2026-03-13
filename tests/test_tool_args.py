import json
import unittest

from oy_cli import parse_tool_arguments


class ParseToolArgumentsTests(unittest.TestCase):
    def test_parses_normal_json_object(self) -> None:
        payload = '{"path":"src/oy_cli/__init__.py","replace_all":false}'

        result = parse_tool_arguments(payload)

        self.assertEqual(
            result,
            {"path": "src/oy_cli/__init__.py", "replace_all": False},
        )

    def test_recovers_from_exactly_doubled_payload(self) -> None:
        payload = '{"n":5}{"n":5}'

        result = parse_tool_arguments(payload)

        self.assertEqual(result, {"n": 5})

    def test_decodes_double_encoded_json(self) -> None:
        payload = json.dumps('{"path":"src/oy_cli/__init__.py","replace_all":false}')

        result = parse_tool_arguments(payload)

        self.assertEqual(
            result,
            {"path": "src/oy_cli/__init__.py", "replace_all": False},
        )


if __name__ == "__main__":
    unittest.main()
