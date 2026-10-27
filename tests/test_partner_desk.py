"""Partner Desk pop-up tests. Stub S3 + stub HTTP only; run with
python3.12 -m unittest discover -s tests"""
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "test-pd-secret"
IDENTITY_SECRET = "test-identity-secret"
ADMIN = "test-admin-key"


class NoSuchKey(Exception):
    pass


class FakeS3:
    """get_object/head_object from OBJECTS; every other call is recorded as a write."""
    objects = {}
    writes = []
    gets = []

    def get_object(self, Bucket, Key):
        FakeS3.gets.append(Key)
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(f"{Bucket}/{Key}")
        return {"Body": io.BytesIO(json.dumps(self.objects[(Bucket, Key)]).encode()),
                "ETag": '"e"'}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(f"{Bucket}/{Key}")
        return {"ETag": '"%d"' % hash(json.dumps(self.objects[(Bucket, Key)]))}

    def __getattr__(self, name):
        def rec(*a, **k):
            FakeS3.writes.append((name, a, k))
            raise AssertionError(f"unexpected S3 call {name}")
        return rec


FakeS3.objects[("pipeline-token", "pipeline-jwt.json")] = {"jwt": "x"}
sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: FakeS3())
os.environ["IDENTITY_SECRET"] = IDENTITY_SECRET
os.environ["ADMIN_KEY"] = ADMIN
sys.path.insert(0, ROOT)
import lambda_function as lf  # noqa: E402

DEAL = {"id": 999, "name": "Acme Sell", "company_id": 5, "updated_at": "2026/10/01 10:00:00 -0400",
        "custom_fields": {}, "deal_stage": {"name": "Firm"}}
COMPANY = {"id": 5, "name": "Acme"}


def fake_urlopen(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.endswith("/deals/999"):
        return io.BytesIO(json.dumps(DEAL).encode())
    if url.endswith("/companies/5"):
        return io.BytesIO(json.dumps(COMPANY).encode())
    raise urllib.error.URLError("offline")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


urllib.request.urlopen = lambda req, *a, **k: _Resp(fake_urlopen(req).getvalue())

PID = "4242"
OTHER = "5151"


def tok(pid=PID, exp=None, secret=SECRET):
    exp = exp if exp is not None else int(time.time()) + 3600
    sig = hmac.new(secret.encode(), f"{pid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{pid}.{exp}.{sig}"


def gg_id(email):
    sig = hmac.new(IDENTITY_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{email}|{sig}".encode()).decode().rstrip("=")


def event(qp=None, cookies=None):
    e = {"version": "2.0", "rawPath": "/deal/999", "rawQueryString": "",
         "queryStringParameters": dict({"deal_id": "999"}, **(qp or {})),
         "headers": {}}
    if cookies:
        e["cookies"] = [f"{k}={v}" for k, v in cookies.items()]
    return e


def set_access(people):
    FakeS3.objects[("full-pipeline-cache", "partner-desk/access.json")] = {"people": people}


def reset(secret=True, access=True, slim=None):
    FakeS3.objects.pop(("pipeline-token", "partner-desk-secret.json"), None)
    FakeS3.objects.pop(("full-pipeline-cache", "partner-desk/access.json"), None)
    FakeS3.objects.pop(("full-pipeline-cache", "syndicate-dash/people-slim.json"), None)
    if secret:
        FakeS3.objects[("pipeline-token", "partner-desk-secret.json")] = {"secret": SECRET}
    if access:
        set_access({PID: {"intermediary": True, "pd": 7218530},
                    OTHER: {"intermediary": False, "pd": None}})
    if slim is not None:
        FakeS3.objects[("full-pipeline-cache", "syndicate-dash/people-slim.json")] = {"people": slim}
    lf._pd_secret_cache.update({"secret": None, "failed_at": 0.0, "error": None})
    lf._pd_access_cache.update({"people": None, "loaded_at": 0.0, "failed_at": 0.0, "error": None})
    FakeS3.gets.clear()
    lf._people_slim_cache.update({"etag": None, "checked_at": 0.0, "by_id": None, "by_email": None})
    FakeS3.writes.clear()


def run(qp=None, cookies=None):
    return lf.lambda_handler(event(qp, cookies), None)


BASE_HEADERS = {"Content-Type": "text/html"}


class PartnerDeskTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.baseline = run()
        reset()

    def tearDown(self):
        self.assertEqual(FakeS3.writes, [], "no S3 writes allowed")

    def assertBaseline(self, r):
        self.assertEqual(r, self.baseline)
        self.assertEqual(r["headers"], BASE_HEADERS)
        self.assertNotIn("pdOverlay", r["body"])
        self.assertNotIn("cookies", r)

    def test_baseline_shape(self):
        self.assertEqual(self.baseline["statusCode"], 200)
        self.assertEqual(self.baseline["headers"], BASE_HEADERS)
        self.assertNotIn("pdOverlay", self.baseline["body"])

    def test_valid_query_token_shows_popup_and_sets_cookie(self):
        t = tok()
        r = run({"gp": t})
        self.assertIn('id="pdOverlay"', r["body"])
        self.assertIn("This deal is not available to co-brokers or foreign finders.", r["body"])
        self.assertIn(f"view=partner-desk&amp;gp={t}", r["body"])
        self.assertIn("investor-qualification-form-for-natural-persons", r["body"])
        self.assertIn("investor-qualification-form-for-entity-persons", r["body"])
        self.assertIn("sendBeacon", r["body"])
        self.assertIn("setTimeout", r["body"])
        self.assertEqual(r["headers"]["Cache-Control"], "private, no-store")
        self.assertEqual(r["cookies"], [f"gg_pd={t}; Path=/; Max-Age=2592000; Secure; HttpOnly; SameSite=Lax"])
        # Everything before the injection is the untouched baseline page.
        i = r["body"].index("\n<style>\n#pdOverlay")
        self.assertEqual(r["body"][:i] + r["body"][r["body"].rindex("</script>") + 9:], self.baseline["body"])

    def test_cookie_alone_shows_popup_without_resetting_cookie(self):
        r = run(cookies={"gg_pd": tok()})
        self.assertIn('id="pdOverlay"', r["body"])
        self.assertEqual(r["headers"]["Cache-Control"], "private, no-store")
        self.assertNotIn("cookies", r)

    def test_rest_v1_event_uses_set_cookie_header(self):
        t = tok()
        e = event({"gp": t})
        for k in ("version", "rawPath"):
            e.pop(k)
        r = lf.lambda_handler(e, None)
        self.assertTrue(r["headers"]["Set-Cookie"].startswith(f"gg_pd={t};"))

    def test_no_popup_cases(self):
        cases = {
            "investor": lambda: set_access({PID: {"intermediary": True, "pd": 7218523}}),
            "partner_registered": lambda: set_access({PID: {"intermediary": True, "pd": "7218528"}}),
            "non_intermediary": lambda: set_access({PID: {"intermediary": False, "pd": None}}),
            "not_listed": lambda: set_access({}),
            "missing_secret": lambda: reset(secret=False),
            "missing_access": lambda: reset(access=False),
        }
        for name, prep in cases.items():
            with self.subTest(name):
                reset(); prep()
                self.assertBaseline(run({"gp": tok()}))
                reset(); prep()
                self.assertBaseline(run(cookies={"gg_pd": tok()}))

    def test_expired_and_bad_sig(self):
        for name, t in {"expired": tok(exp=int(time.time()) - 1),
                        "bad_sig": tok(secret="wrong"),
                        "garbage": "abc", "html": '1.2.<script>'}.items():
            with self.subTest(name):
                reset()
                self.assertBaseline(run({"gp": t}))

    def test_gg_id_other_person_blocks_and_expires_cookie(self):
        slim = [{"id": int(OTHER), "email": "client@example.com", "custom_fields": {}},
                {"id": int(PID), "email": "broker@example.com", "custom_fields": {}}]
        reset(slim=slim)
        t = tok()
        r = run({"gp": t}, cookies={"gg_id": gg_id("client@example.com"), "gg_pd": t})
        self.assertNotIn("pdOverlay", r["body"])
        self.assertEqual(r["cookies"], ["gg_pd=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"])
        self.assertEqual(r["headers"]["Cache-Control"], "private, no-store")
        self.assertEqual(r["body"], self.baseline_signed_in("client@example.com", slim)["body"])
        # Without an existing gg_pd: nothing set, headers untouched.
        reset(slim=slim)
        r = run({"gp": t}, cookies={"gg_id": gg_id("client@example.com")})
        self.assertNotIn("cookies", r)
        self.assertEqual(r["headers"], BASE_HEADERS)
        self.assertNotIn("pdOverlay", r["body"])

    def test_gg_id_unresolvable_is_mismatch(self):
        reset()  # no people-slim, no email in access.json
        r = run({"gp": tok()}, cookies={"gg_id": gg_id("someone@example.com")})
        self.assertNotIn("pdOverlay", r["body"])
        self.assertNotIn("cookies", r)

    def test_gg_id_same_person_shows_popup(self):
        slim = [{"id": int(PID), "email": "Broker@Example.com", "custom_fields": {}}]
        reset(slim=slim)
        r = run({"gp": tok()}, cookies={"gg_id": gg_id("broker@example.com")})
        self.assertIn('id="pdOverlay"', r["body"])
        self.assertTrue(r["cookies"][0].startswith("gg_pd="))

    def test_gg_id_same_person_via_access_json_email(self):
        set_access({PID: {"intermediary": True, "pd": None, "email": "broker@example.com"}})
        r = run({"gp": tok()}, cookies={"gg_id": gg_id("broker@example.com")})
        self.assertIn('id="pdOverlay"', r["body"])

    def test_preview_requires_key(self):
        r = run({"pd_preview": "1", "key": ADMIN})
        self.assertIn('id="pdOverlay"', r["body"])
        self.assertIn("PD preview &mdash; secret: OK, access.json: OK (2 people)", r["body"])
        self.assertIn("var TOKEN = \"\"", r["body"])
        self.assertEqual(r["headers"]["Cache-Control"], "private, no-store")
        self.assertNotIn("cookies", r)
        reset(secret=False, access=False)
        r = run({"pd_preview": "1", "key": ADMIN})
        self.assertIn("secret: FAIL (NoSuchKey", r["body"])
        self.assertIn("access.json: FAIL (NoSuchKey", r["body"])
        for bad in ({"pd_preview": "1"}, {"pd_preview": "1", "key": "nope"}, {"pd_preview": "1", "admin_key": "x"}):
            with self.subTest(bad):
                reset()
                r = run(bad)
                self.assertBaseline(r)

    def test_no_pid_in_page(self):
        set_access({PID: {"intermediary": True, "pd": None, "name": "Bob Broker", "email": "bob@x.com"}})
        t = tok()
        r = run({"gp": t})
        body = r["body"].replace(t, "")
        self.assertNotIn("Bob Broker", body)
        self.assertNotIn("bob@x.com", body)
        self.assertNotIn(PID, body.replace(self.baseline["body"], ""))

    def test_internal_error_falls_back(self):
        orig = lf._pd_render_popup
        lf._pd_render_popup = lambda *a: 1 / 0
        try:
            self.assertBaseline(run({"gp": tok()}))
        finally:
            lf._pd_render_popup = orig

    # --- secret / access.json caching ---
    def age(self, cache, field, seconds):
        cache[field] -= seconds

    def test_secret_missing_then_present_after_retry(self):
        reset(secret=False)
        t = tok()
        self.assertBaseline(run({"gp": t}))
        FakeS3.objects[("pipeline-token", "partner-desk-secret.json")] = {"secret": SECRET}
        self.assertBaseline(run({"gp": t}))          # within 60s: no re-read
        self.assertEqual(FakeS3.gets.count("partner-desk-secret.json"), 1)
        self.age(lf._pd_secret_cache, "failed_at", 61)
        self.assertIn('id="pdOverlay"', run({"gp": t})["body"])
        self.assertEqual(FakeS3.gets.count("partner-desk-secret.json"), 2)

    def test_secret_read_once_and_cached(self):
        for _ in range(3):
            self.assertIn('id="pdOverlay"', run({"gp": tok()})["body"])
        self.assertEqual(FakeS3.gets.count("partner-desk-secret.json"), 1)

    def test_secret_failure_not_permanent_after_access_denied(self):
        orig = FakeS3.get_object
        def denied(self_, Bucket, Key):
            if Key == "partner-desk-secret.json":
                FakeS3.gets.append(Key)
                raise PermissionError("AccessDenied")
            return orig(self_, Bucket, Key)
        FakeS3.get_object = denied
        try:
            self.assertBaseline(run({"gp": tok()}))
        finally:
            FakeS3.get_object = orig
        self.age(lf._pd_secret_cache, "failed_at", 61)
        self.assertIn('id="pdOverlay"', run({"gp": tok()})["body"])

    def test_access_failure_retried_after_60s(self):
        reset(access=False)
        t = tok()
        self.assertBaseline(run({"gp": t}))
        reset_access = lambda: set_access({PID: {"intermediary": True, "pd": None}})
        reset_access()
        self.assertBaseline(run({"gp": t}))          # within 60s: no re-read
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 1)
        self.age(lf._pd_access_cache, "failed_at", 61)
        self.assertIn('id="pdOverlay"', run({"gp": t})["body"])
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 2)

    def test_access_good_copy_cached_15_min(self):
        t = tok()
        run({"gp": t}); run({"gp": t})
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 1)
        self.age(lf._pd_access_cache, "loaded_at", 15 * 60 + 1)
        run({"gp": t})
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 2)

    def test_access_failure_keeps_previous_copy(self):
        t = tok()
        self.assertIn('id="pdOverlay"', run({"gp": t})["body"])
        FakeS3.objects.pop(("full-pipeline-cache", "partner-desk/access.json"))
        self.age(lf._pd_access_cache, "loaded_at", 15 * 60 + 1)
        self.assertIn('id="pdOverlay"', run({"gp": t})["body"])   # refresh fails, old copy used
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 2)
        self.assertIn('id="pdOverlay"', run({"gp": t})["body"])   # within 60s: no re-read
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 2)
        set_access({PID: {"intermediary": False, "pd": None}})
        self.age(lf._pd_access_cache, "failed_at", 61)
        self.assertBaseline(run({"gp": t}))                         # refresh succeeds, new copy used
        self.assertEqual(FakeS3.gets.count("partner-desk/access.json"), 3)

    # helpers
    def baseline_signed_in(self, email, slim):
        reset(slim=slim)
        r = run(cookies={"gg_id": gg_id(email)})
        return r


if __name__ == "__main__":
    unittest.main()
