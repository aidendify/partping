"""Local Flask test client covering PRD section 12 as much as possible without Compose."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Ensure env before importing app
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["BUSINESS_PHONE"] = ""
os.environ["MARKETING_URL"] = ""
os.environ.pop("SMTP_HOST", None)
os.environ.pop("SMTP_PORT", None)
os.environ.pop("SMTP_USER", None)
os.environ.pop("SMTP_PASSWORD", None)
os.environ.pop("FROM_NAME", None)
os.environ.pop("FROM_EMAIL", None)
os.environ.pop("OWNER_NOTIFY_EMAIL", None)
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_AUTH_TOKEN", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)


class PartPingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        os.environ["DATABASE_PATH"] = db_path
        os.environ["OWNER_PASSWORD"] = "testpass"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
        os.environ["BUSINESS_NAME"] = "Harbor HVAC"
        os.environ["MARKETING_URL"] = ""
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        os.environ.pop("TWILIO_FROM_NUMBER", None)
        os.environ.pop("OWNER_NOTIFY_EMAIL", None)

        import importlib
        import app as app_module
        import helpers as helpers_module

        importlib.reload(helpers_module)
        importlib.reload(app_module)
        self.app_module = app_module
        self.helpers = helpers_module
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            app_module.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login(self) -> None:
        r = self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    def _create_job(
        self,
        part_name: str = "TXV valve",
        customer_name: str = "Priya Sharma",
        **extra,
    ) -> tuple[int, str]:
        self._login()
        data = {
            "customer_name": customer_name,
            "customer_email": extra.get("customer_email", "priya.sharma@example.com"),
            "customer_phone": extra.get("customer_phone", "+15550142"),
            "job_ref": extra.get("job_ref", "WO-9182"),
            "part_name": part_name,
            "distributor": extra.get("distributor", "Johnstone"),
            "notes": extra.get("notes", "Counter pickup"),
            "eta_date": extra.get("eta_date", ""),
        }
        r = self.client.post("/jobs/new", data=data, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get("Location", "")
        self.assertIn("/jobs/", loc)
        job_id = int(loc.rstrip("/").split("/")[-1])
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertIsNotNone(job)
            return job_id, job["token"]

    def test_01_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["sms_configured"], False)

    def test_02_auth_gate(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/login", r.headers.get("Location", ""))

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        job_id, token = self._create_job()
        self.client.get("/logout")
        r = self.client.get(f"/p/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"TXV valve", r.data)

        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

        r = self.client.get("/closed", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_03_create_job(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/p/{token}".encode(), r.data)
        self.assertIn(b"TXV valve", r.data)
        self.assertIn(b"Priya Sharma", r.data)
        self.assertIn(b"Part ordered", r.data)
        self.assertIn(b"Copy link", r.data)

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["milestone"], "ordered")
            events = self.app_module.list_events(job_id)
            kinds = [e["kind"] for e in events]
            self.assertIn("created", kinds)

    def test_04_public_page(self) -> None:
        job_id, token = self._create_job()
        self.client.get("/logout")
        r = self.client.get(f"/p/{token}")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode("utf-8")
        self.assertIn("Harbor HVAC", body)
        self.assertIn("TXV valve", body)
        self.assertIn("Priya Sharma", body)
        self.assertIn("Part ordered", body)
        self.assertIn("Questions? Call us.", body)
        self.assertNotIn("Accept", body)
        self.assertNotIn("Decline", body)
        self.assertNotIn("Stripe", body)
        self.assertNotIn("review", body.lower().replace("overview", ""))

        r = self.client.get("/p/not-a-real-token")
        self.assertEqual(r.status_code, 404)

    def test_05_milestone_flip(self) -> None:
        job_id, token = self._create_job()
        r = self.client.post(
            f"/jobs/{job_id}/milestone",
            data={
                "milestone": "shipped",
                "note": "Left supply house",
                "eta_date": "2030-01-15",
                "notify": "0",
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["milestone"], "shipped")
            self.assertEqual(job["eta_date"], "2030-01-15")
            events = self.app_module.list_events(job_id)
            milestone_events = [e for e in events if e["kind"] == "milestone"]
            self.assertEqual(len(milestone_events), 1)
            self.assertEqual(milestone_events[0]["milestone"], "shipped")
            self.assertEqual(milestone_events[0]["note"], "Left supply house")

        r = self.client.get(f"/p/{token}")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode("utf-8")
        self.assertIn("Part shipped", body)
        self.assertIn("Left supply house", body)
        self.assertIn("2030", body)
        self.assertIn("ETA:", body)

    def test_06_idempotent_same_milestone(self) -> None:
        job_id, token = self._create_job()
        self.client.post(
            f"/jobs/{job_id}/milestone",
            data={
                "milestone": "shipped",
                "note": "Left supply house",
                "eta_date": "2030-01-15",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            before = len(
                [
                    e
                    for e in self.app_module.list_events(job_id)
                    if e["kind"] == "milestone"
                ]
            )

        r = self.client.post(
            f"/jobs/{job_id}/milestone",
            data={
                "milestone": "shipped",
                "note": "",
                "eta_date": "2030-01-15",
            },
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"No change", r.data)

        with self.app.app_context():
            after = len(
                [
                    e
                    for e in self.app_module.list_events(job_id)
                    if e["kind"] == "milestone"
                ]
            )
            self.assertEqual(before, after)

    def test_07_terminal_closed(self) -> None:
        job_id, token = self._create_job()
        self.client.post(
            f"/jobs/{job_id}/milestone",
            data={"milestone": "closed", "note": "", "eta_date": ""},
            follow_redirects=False,
        )

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Priya Sharma", r.data)

        r = self.client.get("/closed")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Priya Sharma", r.data)
        self.assertIn(b"TXV valve", r.data)

        r = self.client.get(f"/p/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Job moved on", r.data)

    def test_08_no_twilio_smtp_required(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Copy link", r.data)
        body = r.data.decode("utf-8")
        self.assertIn("Copy link", body)
        self.assertNotIn('name="notify"', body)

        r = self.client.get("/health")
        self.assertIs(r.get_json()["smtp_configured"], False)
        self.assertIs(r.get_json()["sms_configured"], False)

    def test_09_out_of_scope_guard(self) -> None:
        src = Path(__file__).resolve().parent
        app_text = (src / "app.py").read_text()
        helpers_text = (src / "helpers.py").read_text()
        combined = app_text + helpers_text
        for banned in (
            "stripe",
            "Stripe",
            "celery",
            "redis",
            "Redis",
            "openai",
            "langchain",
            "service_titan",
            "johnstone_api",
            "leaflet",
            "google.maps",
            "CSAT",
            "drip",
        ):
            self.assertNotIn(banned.lower() if banned.islower() else banned, combined)

        with self.app.app_context():
            db = self.app_module.get_db()
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("jobs", tables)
        self.assertIn("events", tables)
        self.assertNotIn("sequences", tables)
        self.assertNotIn("drips", tables)
        self.assertNotIn("leads", tables)
        self.assertTrue(tables <= {"jobs", "events", "sqlite_sequence"})

        _, token = self._create_job()
        r = self.client.get(f"/p/{token}")
        body = r.data.decode("utf-8")
        self.assertNotIn("Accept", body)
        self.assertNotIn("Upload", body)
        self.assertNotIn("map", body.lower())

    def test_10_empty_marketing_url_no_footer(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get(f"/p/{token}")
        self.assertNotIn(b"Powered by PartPing", r.data)
        r = self.client.get("/")
        self.assertNotIn(b"Powered by PartPing", r.data)

    def test_11_paste_stores_only(self) -> None:
        job_id, _token = self._create_job()
        r = self.client.post(
            f"/jobs/{job_id}/paste",
            data={"raw_paste": "Tracking ABC ETA maybe next week"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["milestone"], "ordered")
            events = self.app_module.list_events(job_id)
            pastes = [e for e in events if e["kind"] == "paste"]
            self.assertEqual(len(pastes), 1)
            self.assertIn("Tracking ABC", pastes[0]["raw_paste"])

    def test_12_notify_failure_does_not_rollback(self) -> None:
        """Milestone stays even if notify would fail (SMTP unset + checked is skipped)."""
        job_id, _token = self._create_job()
        self.client.post(
            f"/jobs/{job_id}/milestone",
            data={
                "milestone": "arrived",
                "note": "On shelf",
                "eta_date": "2030-02-01",
                "notify": "1",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["milestone"], "arrived")
            self.assertEqual(job["eta_date"], "2030-02-01")


if __name__ == "__main__":
    unittest.main()
