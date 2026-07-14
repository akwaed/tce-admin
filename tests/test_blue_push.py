"""Unit tests for the rebuilt Blue push module (no live SOAP)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.blue_push.config import (  # noqa: E402
    DEFAULT_DATASOURCES,
    DEFAULT_IMPORT_ORDER,
    MIN_NON_USERS_GAP_SECONDS,
)
from app.services.blue_push.csv_loader import load_datasource_csv  # noqa: E402
from app.services.blue_push.soap import (  # noqa: E402
    check_response,
    escape_xml,
    extract_value,
    soap_prepare_finalize,
    soap_register_import,
)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class TestConfig(unittest.TestCase):
    def test_users_is_last_in_default_order(self):
        self.assertEqual(DEFAULT_IMPORT_ORDER[-1], "users")
        self.assertTrue(DEFAULT_DATASOURCES["users"].is_users)
        self.assertEqual(DEFAULT_DATASOURCES["users"].datasource_id, "Data144")

    def test_known_datasource_ids(self):
        self.assertEqual(DEFAULT_DATASOURCES["courses"].datasource_id, "Data161")
        self.assertEqual(DEFAULT_DATASOURCES["instructors"].datasource_id, "Data162")
        self.assertEqual(DEFAULT_DATASOURCES["students"].datasource_id, "Data163")

    def test_users_column_remap(self):
        m = DEFAULT_DATASOURCES["users"].column_map
        self.assertEqual(m["FIRSTNAME"], "FIRSTNAME_1")
        self.assertEqual(m["LASTNAME"], "LASTNAME_1")

    def test_prepare_timeout_constant(self):
        from app.services.blue_push.config import TIMEOUT_PREPARE, TIMEOUT_FINALIZE
        self.assertEqual(TIMEOUT_PREPARE, 600)
        self.assertEqual(TIMEOUT_FINALIZE, 300)
        self.assertEqual(MIN_NON_USERS_GAP_SECONDS, 180)


class TestCsvLoader(unittest.TestCase):
    def test_hash_dropped_and_names_remapped(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Users.csv"
            path.write_text(
                "USER_ID,UKID_NBR,STU_OBJ_ID,FIRSTNAME,LASTNAME,EMAIL,BLUE_ROLE,HASH\n"
                "u1,100,200,Ann,Lee,a@x.com,23,<object at 0xdead>\n"
                "u2,101,201,Bob,Kay,b@x.com,528,<object at 0xbeef>\n",
                encoding="utf-8",
            )
            loaded = load_datasource_csv(str(path), DEFAULT_DATASOURCES["users"])
            self.assertEqual(loaded.total_rows, 2)
            self.assertIn("HASH", loaded.dropped_columns)
            self.assertEqual(
                loaded.columns,
                [
                    "USER_ID",
                    "UKID_NBR",
                    "STU_OBJ_ID",
                    "FIRSTNAME_1",
                    "LASTNAME_1",
                    "EMAIL",
                    "BLUE_ROLE",
                ],
            )
            self.assertEqual(
                loaded.rows[0],
                ["u1", "100", "200", "Ann", "Lee", "a@x.com", "23"],
            )
            self.assertEqual(loaded.rows[1][-1], "528")
            self.assertNotIn("HASH", loaded.columns)
            self.assertIn("BLUE_ROLE", DEFAULT_DATASOURCES["users"].columns)
            self.assertIn("UKID_NBR", DEFAULT_DATASOURCES["users"].columns)
            self.assertIn("STU_OBJ_ID", DEFAULT_DATASOURCES["users"].columns)

    def test_only_present_columns_used(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Instructor_Course.csv"
            path.write_text(
                "SECTION_KEY,USER_ID\ns1,u1\n",
                encoding="utf-8",
            )
            loaded = load_datasource_csv(
                str(path), DEFAULT_DATASOURCES["instructors"]
            )
            self.assertEqual(loaded.columns, ["SECTION_KEY", "USER_ID"])
            self.assertEqual(loaded.rows[0], ["s1", "u1"])


class TestSoapHelpers(unittest.TestCase):
    def test_escape_xml(self):
        self.assertEqual(escape_xml('a&b<"\''), "a&amp;b&lt;&quot;&apos;")

    def test_register_payload_contains_datasource(self):
        xml = soap_register_import("KEY", "Data161")
        self.assertIn("Data161", xml)
        self.assertIn("KEY", xml)

    def test_prepare_uses_transaction_id(self):
        xml = soap_prepare_finalize("KEY", "TID-99")
        self.assertIn("TID-99", xml)

    def test_check_response_success(self):
        r = FakeResponse(
            text="<Result>true</Result><Message>ok</Message>"
        )
        ok, msg, _ = check_response(r, "RegisterImport")
        self.assertTrue(ok)

    def test_extract_value(self):
        text = "<a:TransactionID>abc-123</a:TransactionID>"
        self.assertEqual(extract_value(text, "TransactionID"), "abc-123")


class TestOrderingHelper(unittest.TestCase):
    def test_users_moved_last(self):
        # Mirror orchestrator ordering logic without DB
        configs = [
            DEFAULT_DATASOURCES["users"],
            DEFAULT_DATASOURCES["courses"],
            DEFAULT_DATASOURCES["students"],
        ]
        users = [c for c in configs if c.is_users or c.datasource_id == "Data144"]
        others = [c for c in configs if not (c.is_users or c.datasource_id == "Data144")]
        ordered = others + users
        self.assertEqual(ordered[-1].key, "users")
        self.assertEqual([c.key for c in ordered], ["courses", "students", "users"])


if __name__ == "__main__":
    unittest.main()
