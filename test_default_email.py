import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import default_email as app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=app.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(app, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.output_patch = patch("sys.stdout", new_callable=io.StringIO)
        self.output_patch.start()
        self.addCleanup(self.output_patch.stop)
        self.users = {
            "1130000000000001": {"id": "1130000000000001", "email": "ivan@example.org",
                "default_email": "ivan@example.org", "name": {"last": "Иванов", "first": "Иван"}},
            "1130000000000002": {"id": "1130000000000002", "email": "anna@example.org",
                "default_email": "anna@example.org", "name": {"first": "Анна"}}}
        self.client = Mock()
        self.client.users.return_value = self.users
        self.path = app.export_users(self.client, "123")

    def edit(self, **changes):
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        rows[0].update(changes)
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=app.FIELDS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

    def test_export_unicode_and_empty_targets(self):
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("Иванов Иван", self.path.read_text(encoding="utf-8-sig"))
        plan = app.read_plan(self.path, "123", self.users)
        self.assertEqual([item[2] for item in plan], ["skipped_empty"] * 2)

    def test_plan_and_resume(self):
        self.edit(default_email="ivan@example.com")
        plan = app.read_plan(self.path, "123", self.users)
        self.assertEqual(plan[0][2], "pending")
        self.users[plan[0][0]]["default_email"] = "ivan@example.com"
        self.assertEqual(app.read_plan(self.path, "123", self.users)[0][2], "skipped_equal")

    def test_rejects_wrong_org_uid_and_service_columns(self):
        with self.assertRaises(app.UserError):
            app.read_plan(self.path, "999", self.users)
        for changes in ({"id": "99"}, {"id": "1130000000000002"},
                        {"email": "other@example.org"}, {"name": "Other"}):
            with self.subTest(changes=changes):
                original = self.path.read_bytes()
                self.edit(**changes)
                with self.assertRaises(app.UserError):
                    app.read_plan(self.path, "123", self.users)
                self.path.write_bytes(original)

    def test_rejects_conflict_and_bad_address(self):
        self.edit(default_email="bad address")
        with self.assertRaises(app.UserError):
            app.read_plan(self.path, "123", self.users)
        self.edit(default_email="ivan@example.com")
        self.users["1130000000000001"]["default_email"] = "changed@example.org"
        with self.assertRaises(app.UserError):
            app.read_plan(self.path, "123", self.users)

    def test_partial_errors_continue(self):
        self.client.patch.side_effect = [app.ApiError("HTTP 400", 400),
                                        {"id": "2", "default_email": "b@example.org"}]
        report = self.root / "results.jsonl"
        result = app.execute(self.client, [("1", "a@example.org", "pending"),
                                          ("2", "b@example.org", "pending")], report)
        self.assertEqual(result, 1)
        records = [json.loads(line) for line in report.read_text(encoding="utf-8-sig").splitlines()]
        self.assertEqual([r["status"] for r in records], ["started", "failed", "started", "success"])

    def test_uncertain_or_auth_failure_stops(self):
        for index, error in enumerate([app.ApiError("timeout", uncertain=True),
                                      app.ApiError("HTTP 403", 403), app.ApiError("HTTP 429", 429)]):
            with self.subTest(error=error):
                client = Mock()
                client.patch.side_effect = error
                self.assertEqual(app.execute(client, [("1", "a@example.org", "pending"),
                                                     ("2", "b@example.org", "pending")],
                                             self.root / f"report{index}.jsonl"), 1)
                client.patch.assert_called_once()

    def test_dry_run_and_cancel_never_patch(self):
        self.edit(default_email="ivan@example.com")
        with patch.object(app, "Client", return_value=self.client), patch.dict(app.os.environ, {"Y360_TOKEN": "test"}):
            self.assertEqual(app.main(["--org-id", "123", "--apply", self.path.name, "--dry-run"]), 0)
            with patch("builtins.input", return_value="NO"):
                self.assertEqual(app.main(["--org-id", "123", "--apply", self.path.name]), 0)
        self.client.patch.assert_not_called()

    def test_successful_cli(self):
        self.edit(default_email="ivan@example.com")
        self.client.patch.return_value = {"id": "1130000000000001", "default_email": "ivan@example.com"}
        with patch.object(app, "Client", return_value=self.client), patch.dict(app.os.environ, {"Y360_TOKEN": "test"}), patch("builtins.input", return_value="APPLY 123 1"):
            self.assertEqual(app.main(["--org-id", "123", "--apply", self.path.name]), 0)
        self.client.patch.assert_called_once_with("1130000000000001", "ivan@example.com")

    def test_paths_and_formula_cells(self):
        with self.assertRaises(app.UserError):
            app.project_path("../elsewhere.csv")
        self.assertEqual(app.safe_cell("=1+1"), "'=1+1")


class ClientTests(unittest.TestCase):
    def test_pagination_uses_response_limit(self):
        client = app.Client("123", "test")
        client.request = Mock(side_effect=[
            {"items": [{"id": "1"}, {"id": "2"}], "total": 3, "limit": 2, "offset": 0},
            {"items": [{"id": "3"}], "total": 3, "limit": 2, "offset": 2}])
        self.assertEqual(list(client.users()), ["1", "2", "3"])
        self.assertIn("offset=2", client.request.call_args.args[1])

    def test_inconsistent_page_rejected(self):
        for page in [{"items": [], "total": 1, "limit": 100, "offset": 0},
                     {"items": [{"id": "1"}, {"id": "1"}], "total": 2, "limit": 100, "offset": 0}]:
            client = app.Client("123", "test")
            client.request = Mock(return_value=page)
            with self.assertRaises(app.UserError):
                client.users()

    def test_patch_exact_contract(self):
        client = app.Client("123", "test")
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"id":"1","default_email":"a@example.org"}'))
        response.__exit__ = Mock(return_value=False)
        client.opener = Mock()
        client.opener.open.return_value = response
        client.patch("1", "a@example.org")
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.full_url, app.BASE_URL + "/123/users/1")
        self.assertEqual(json.loads(request.data), {"default_email": "a@example.org"})
        self.assertEqual(request.get_header("Authorization"), "OAuth test")

    @patch.object(app.time, "sleep")
    def test_get_retries_patch_does_not(self, sleep):
        client = app.Client("123", "test")
        client.opener = Mock()
        client.opener.open.side_effect = URLError("offline")
        with self.assertRaises(app.ApiError):
            client.request("GET")
        self.assertEqual(client.opener.open.call_count, 3)
        client.opener.open.reset_mock()
        with self.assertRaises(app.ApiError) as context:
            client.patch("1", "a@example.org")
        self.assertTrue(context.exception.uncertain)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_http_auth_has_no_retry(self):
        client = app.Client("123", "test")
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError(client.url, 401, "Denied", {}, None)
        with self.assertRaises(app.ApiError) as context:
            client.request("GET")
        self.assertEqual(context.exception.status, 401)
        client.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
