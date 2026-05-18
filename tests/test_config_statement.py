import json
import tempfile
import unittest
from pathlib import Path

from inventory_app.config import load_config


class ConfigStatementTests(unittest.TestCase):
    def test_loads_statement_customer_name(self) -> None:
        data = {
            "smartstore": {
                "client_id": "id",
                "client_secret": "secret",
                "token_type": "Bearer",
            },
            "coupang": {
                "vendor_id": "vendor",
                "access_key": "access",
                "secret_key": "secret",
            },
            "request": {"timeout_seconds": 10, "max_products": 100},
            "statement": {"customer_name": "ex-tracker"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            cfg = load_config(path)

        self.assertEqual(cfg.statement_customer_name, "ex-tracker")


if __name__ == "__main__":
    unittest.main()
