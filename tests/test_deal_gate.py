"""Deal gate tests. Fixtures only: stub S3 and stub HTTP, no live S3/SES/Pipeline.
python3.12 -m unittest discover -s tests

The module is loaded under its own name with its own boto3 stub, so these tests
never share state with the other test files."""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
IDENTITY_SECRET = "test-identity-secret"
ADMIN = "test-admin-key"
VIEW_SECRET = "test-view-secret"
PD_SECRET = "test-pd-secret"
B = "full-pipeline-cache"
SLIM = "syndicate-dash/people-slim.json"
NOW = 1_900_000_000          # fixed clock for token tests

# Fixture deal: every withheld value is distinctive so a body search proves absence.
DEAL_ID = "4242"
DEAL_NAME = "Acme: Series Q Zebra SPV Lot"
NET, GROSS, SHARES = 31.17, 33.29, 98765
MIN_SIZE, MAX_SIZE = 271000, 838000
NOTES = "Quokka allocation note"
MGMT, CARRY, DEADLINE = 2.75, 17.5, "2031/07/19"
SECRET_STRINGS = ["31.17", "33.29", "98,765", "98765", "271,000", "838,000", "271K", "838K",
                  "Quokka", "Zebra", "Series Q", "2.75", "17.5", "2031-07-19", "Fund (SPV)",
                  "SPV Details", "Send Question", "px=", "bidLoginModal"]


def _deal():
    return {
        "id": int(DEAL_ID), "name": DEAL_NAME, "company_id": 5,
        "updated_at": "2026/10/01 10:00:00 -0400", "summary": NOTES,
        "deal_stage": {"id": 1, "name": "Firm"},
        "custom_fields": {
            "custom_label_1958": 5011675,            # Sell Order
            "custom_label_3064360": [5077906],       # Structure (Fund)
            "custom_label_3064369": NET, "custom_label_3064339": GROSS,
            "custom_label_3070843": SHARES,
            "custom_label_3065488": MIN_SIZE, "custom_label_3064645": MAX_SIZE,
            "custom_label_3940558": MGMT, "custom_label_3940559": CARRY,
            "custom_label_4006402": DEADLINE,
            "custom_label_3994687": 0,                # Refresh 0: never Unconfirmed
            "custom_label_3064333": 5077849,        # Series C
        },
    }


COMPANY = {"id": 5, "name": "Acme", "description": "Acme builds rockets.",
           "custom_fields": {"custom_label_3064363": 10.0, "custom_label_3790429": 2.0}}

# people-slim.json: one person per Investor Level case
P_CLEARED, P_HOLD, P_NONACC, P_BLANK, P_NEWOPT = "101", "102", "103", "104", "105"
PEOPLE = [
    {"id": 101, "first_name": "Ann", "last_name": "Clear", "email": "ann@x.com",
     "company": {"name": "Ann Co"}, "custom_fields": {"custom_label_3923758": 6950563}},
    {"id": 102, "first_name": "Hal", "last_name": "Hold", "email": "hal@x.com",
     "custom_fields": {"custom_label_3923758": 7161646}},
    {"id": 103, "first_name": "Ned", "last_name": "Non", "email": "ned@x.com",
     "custom_fields": {"custom_label_3923758": [6950562]}},
    {"id": 104, "first_name": "Bea", "last_name": "Blank", "email": "bea@x.com", "custom_fields": {}},
    {"id": 105, "first_name": "Sam", "last_name": "Self", "email": "sam@x.com",
     "custom_fields": {"custom_label_3923758": 9999999}},
    # shared address: one blocked, one cleared -> cleared
    {"id": 106, "first_name": "Two", "email": "two@x.com", "custom_fields": {"custom_label_3923758": 6950562}},
    {"id": 107, "first_name": "Two", "email": "two@x.com", "custom_fields": {"custom_label_3923758": 6950564}},
    # shared address: blocked + hold -> blocked
    {"id": 108, "first_name": "Bh", "email": "bh@x.com", "custom_fields": {"custom_label_3923758": 6950562}},
    {"id": 109, "first_name": "Bh", "email": "bh@x.com", "custom_fields": {"custom_label_3923758": 7161646}},
]


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self):
        self.objects, self.gets, self.puts = {}, [], []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(f"{Bucket}/{Key}")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]), "ETag": '"e"'}

    def head_object(self, Bucket, Key):
        self.gets.append("HEAD " + Key)
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(f"{Bucket}/{Key}")
        return {"ETag": '"%s"' % hashlib.md5(self.objects[(Bucket, Key)]).hexdigest(),
                "LastModified": "2026-11-13T00:00:00Z"}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append(Key)
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else str(Body).encode()


S3 = FakeS3()


def load_module(path, name):
    """Import a lambda_function file under its own name with the stub boto3."""
    saved_boto, saved_env = sys.modules.get("boto3"), dict(os.environ)
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: S3)
    os.environ.update({"IDENTITY_SECRET": IDENTITY_SECRET, "ADMIN_KEY": ADMIN})
    S3.objects[("pipeline-token", "pipeline-jwt.json")] = json.dumps({"jwt": "x"}).encode()
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved_boto is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = saved_boto
        os.environ.clear()
        os.environ.update(saved_env)
    mod.boto3 = types.SimpleNamespace(client=lambda *a, **k: S3)
    return mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, *a, **k):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.endswith("/deals/" + DEAL_ID):
        return _Resp(json.dumps(_deal()).encode())
    if url.endswith("/companies/5"):
        return _Resp(json.dumps(COMPANY).encode())
    raise urllib.error.URLError("offline")


def reset(mod, switch=None, slim=True):
    S3.objects = {("pipeline-token", "pipeline-jwt.json"): json.dumps({"jwt": "x"}).encode()}
    S3.objects[("pipeline-token", "partner-desk-secret.json")] = json.dumps({"secret": PD_SECRET}).encode()
    if switch is not None:
        S3.objects[(B, "deal-gate.json")] = json.dumps({"enabled": switch, "updated_at": "x"}).encode()
    if slim:
        S3.objects[(B, SLIM)] = json.dumps({"people": PEOPLE}).encode()
    S3.gets.clear()
    S3.puts.clear()
    if hasattr(mod, "_dg_switch_cache"):
        mod._dg_switch_cache.update({"enabled": None, "checked_at": 0.0})
        mod._dg_approved_cache.update({"ids": {}, "checked_at": 0.0})
        mod._dg_queue_seen.clear()
        mod._dg_queue_cache.update({"data": None, "checked_at": 0.0})
    mod._people_slim_cache.update({"etag": None, "checked_at": 0.0, "by_id": None, "by_email": None,
                                   "dg_by_id": None, "last_modified": None})
    for c in ("_pd_secret_cache",):
        getattr(mod, c).update({"secret": None, "failed_at": 0.0, "error": None})
    mod._pd_access_cache.update({"people": None, "loaded_at": 0.0, "failed_at": 0.0, "error": None})


def gg_id(email):
    sig = hmac.new(IDENTITY_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{email}|{sig}".encode()).decode().rstrip("=")


def view_tok(pid, prefix, exp, secret=VIEW_SECRET):
    sig = hmac.new(secret.encode(), f"{prefix}|{pid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{pid}.{exp}.{sig}"


def pd_tok(pid, exp):
    sig = hmac.new(PD_SECRET.encode(), f"{pid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{pid}.{exp}.{sig}"


def event(qp=None, cookies=None, v2=True):
    qp = dict(qp or {})
    qp.setdefault("deal_id", DEAL_ID)
    e = {"queryStringParameters": qp, "headers": {}}
    if v2:
        e.update({"version": "2.0", "rawPath": f"/deal/{DEAL_ID}",
                  "rawQueryString": "&".join(f"{k}={v}" for k, v in qp.items())})
    else:
        e.update({"path": f"/deal/{DEAL_ID}"})
    if cookies:
        if v2:
            e["cookies"] = [f"{k}={v}" for k, v in cookies.items()]
        else:
            e["headers"]["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return e


lf = load_module(os.path.join(ROOT, "lambda_function.py"), "crm_lf_dealgate")


def run(mod, qp=None, cookies=None, v2=True, env=None):
    with mock.patch.object(mod.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.dict(os.environ, env if env is not None else {"VIEW_LINK_SECRET": VIEW_SECRET}):
        return mod.lambda_handler(event(qp, cookies, v2), None)


class SwitchOffIdentical(unittest.TestCase):
    """With the switch OFF an anonymous visitor gets exactly today's page."""

    def test_anon_deal_page_matches_pre_gate_golden(self):
        golden = os.path.join(FIXTURES, "crm_deal_anon_golden.html")
        for switch in (None, False):
            reset(lf, switch=switch)
            r = run(lf)
            with open(golden, encoding="utf-8") as f:
                self.assertEqual(r["body"], f.read())
            self.assertEqual(r["headers"], {"Content-Type": "text/html"})
            self.assertNotIn("cookies", r)
            # anonymous: people-slim.json never touched
            self.assertFalse([k for k in S3.gets if SLIM in k])

    def test_switch_off_identified_viewer_gets_full_page_without_lookup(self):
        reset(lf, switch=False)
        r = run(lf, cookies={"gg_id": gg_id("ned@x.com")})
        self.assertIn("31.17", r["body"])
        self.assertNotIn('class="gated"', r["body"])


class ViewerStates(unittest.TestCase):
    def state(self, cookies=None, qp=None):
        reset(lf, switch=True)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            e = event(qp, cookies)
            return lf.dg_decide(e, e["queryStringParameters"],
                                lf._get_cookie(e, "admin_key") == ADMIN)

    def test_states(self):
        exp = int(time.time()) + 3600
        cases = [
            ({}, "anon", True),
            ({"gg_id": gg_id("ann@x.com")}, "cleared", False),
            ({"gg_id": gg_id("hal@x.com")}, "pending", True),
            ({"gg_id": gg_id("ned@x.com")}, "blocked", True),
            ({"gg_id": gg_id("bea@x.com")}, "pending", True),       # blank level
            ({"gg_id": gg_id("sam@x.com")}, "pending", True),       # unlisted option id
            ({"gg_id": gg_id("nobody@x.com")}, "pending", True),    # no matching person
            ({"gg_id": gg_id("two@x.com")}, "cleared", False),      # blocked + cleared
            ({"gg_id": gg_id("bh@x.com")}, "blocked", True),        # blocked + hold
            ({"gg_view": view_tok(P_CLEARED, "viewc1", exp)}, "cleared", False),
            ({"gg_view": view_tok(P_HOLD, "viewc1", exp)}, "cleared", False),   # personal link clears
            ({"gg_view": view_tok(P_BLANK, "viewc1", exp)}, "cleared", False),
            ({"gg_view": view_tok(P_NONACC, "viewc1", exp)}, "blocked", True),
            ({"gg_view": view_tok(P_CLEARED, "view1", exp)}, "anon", True),  # link token != cookie token
            ({"admin_key": ADMIN}, "cleared", False),
        ]
        for cookies, want, gated in cases:
            g, st, prev = self.state(cookies)
            self.assertEqual((st, g), (want, gated), cookies)
            self.assertFalse(prev)

    def test_anonymous_never_loads_people(self):
        self.state({})
        self.assertFalse([k for k in S3.gets if SLIM in k])

    def test_index_unreadable(self):
        reset(lf, switch=True, slim=False)
        e = event(None, {"gg_id": gg_id("ned@x.com")})
        self.assertEqual(lf.dg_decide(e, {}, False)[:2], (False, "cleared"))
        e = event(None, {})
        self.assertEqual(lf.dg_decide(e, {}, False)[:2], (True, "anon"))

    def test_admin_preview_regardless_of_switch(self):
        for switch in (False, True, None):
            for st in ("anon", "pending", "blocked", "cleared"):
                reset(lf, switch=switch)
                r = run(lf, qp={"gate_preview": st}, cookies={"admin_key": ADMIN})
                if st == "cleared":
                    self.assertIn("31.17", r["body"])
                else:
                    self.assertIn('class="gated"', r["body"])
                    for s in ("31.17", "Quokka"):
                        self.assertNotIn(s, r["body"])
                self.assertNotIn("Copy link for", r["body"])   # rendered as that viewer

    def test_preview_ignored_for_non_admin(self):
        reset(lf, switch=False)
        r = run(lf, qp={"gate_preview": "anon"})
        self.assertIn("31.17", r["body"])


class Withheld(unittest.TestCase):
    def body_for(self, cookies=None, qp=None, v2=True):
        reset(lf, switch=True)
        return run(lf, qp=qp, cookies=cookies, v2=v2)

    def test_gated_page_contains_no_real_values(self):
        for cookies in ({}, {"gg_id": gg_id("hal@x.com")}, {"gg_id": gg_id("ned@x.com")}):
            r = self.body_for(cookies)
            body = r["body"]
            for s in SECRET_STRINGS:
                self.assertNotIn(s, body, (cookies, s))
            self.assertIn('class="gated"', body)
            self.assertIn("$00.00", body)
            self.assertIn("Acme", body)
            self.assertIn("Acme builds rockets.", body)          # description kept
            self.assertIn("$10.00", body)                        # Company LR kept
            self.assertIn('<link rel="canonical" href="https://trades.graciagroup.com/deal/4242">', body)
            self.assertNotIn("noindex", body)
            self.assertIn("<title>Acme Pre-IPO Stock – Sell</title>", body)
            self.assertEqual(r["headers"].get("Cache-Control"), "private, no-store")

    def test_gate_box_copy(self):
        body = self.body_for({})["body"]
        self.assertIn("Deal terms are shown to investors we work with", body)
        self.assertIn('<p class="gate-l1">Already a client? Enter your email for a one-click link.</p>', body)
        self.assertIn('<button type="submit">Send link</button>', body)
        self.assertIn("<div class=\"gate-new\"><p>New here? Create a login and we'll review your access.</p>", body)
        self.assertIn('<div class="gate-done" hidden><p>If that address is on file, a link is on its way. '
                      'Not a client yet?</p>', body)
        self.assertEqual(body.count('<a class="gate-btn" href="https://us-east-1dsttcaqx7.auth.us-east-1.amazoncognito.com/signup?'), 2)
        self.assertIn(".gate-btn{display:inline-block;padding:8px 16px;font-size:14px;font-weight:600;", body)
        self.assertNotIn("Already working with us", body)
        self.assertIn("/signup?client_id=", body)
        self.assertIn('name="website"', body)
        for word in ("CRM", "Investor Level", "tracking", "Pipeline"):
            self.assertNotIn(word, body.split("gate-box", 1)[1].split("</div>", 1)[0])
        b = self.body_for({"gg_id": gg_id("ned@x.com")})["body"]
        self.assertIn("Thanks for registering. Your access is being reviewed and we'll be in touch.", b)
        self.assertNotIn("Send link", b)
        b = self.body_for({"gg_id": gg_id("hal@x.com")})["body"]      # Hold: details form instead
        self.assertIn("<h2>Request access to deal terms</h2>", b)
        self.assertIn("Securities rules limit pricing to investors we've reviewed. Tell us who you are and "
                      "we'll reply within one business day.", b.replace("&#x27;", "'"))
        self.assertNotIn("Send link", b)
        for word in ("CRM", "Investor Level", "tracking", "Pipeline"):
            self.assertNotIn(word, b.split("gate-box", 1)[1].split("</div>", 1)[0])

    def test_same_output_for_any_user_agent(self):
        reset(lf, switch=True)
        e1, e2 = event(), event()
        e2["headers"]["user-agent"] = "Mozilla/5.0 (compatible; Googlebot/2.1)"
        with mock.patch.object(lf.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            a = lf.lambda_handler(e1, None)["body"]
            b = lf.lambda_handler(e2, None)["body"]
        self.assertEqual(a, b)

    def test_cleared_viewer_sees_terms(self):
        body = self.body_for({"gg_id": gg_id("ann@x.com")})["body"]
        self.assertIn("31.17", body)
        self.assertIn("Quokka", body)


class Tokens(unittest.TestCase):
    def setUp(self):
        reset(lf)
        self.env = mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_roundtrip_and_format(self):
        tok = lf.dg_make_token(P_CLEARED, now=NOW)
        self.assertEqual(tok, view_tok(P_CLEARED, "view1", NOW + 183 * 86400))
        self.assertEqual(lf.dg_verify_token(tok, now=NOW), P_CLEARED)

    def test_expired(self):
        tok = lf.dg_make_token(P_CLEARED, now=NOW)
        self.assertIsNone(lf.dg_verify_token(tok, now=NOW + 183 * 86400))
        self.assertIsNone(lf.dg_verify_token(tok, now=NOW + 183 * 86400 + 5))

    def test_tampered(self):
        tok = lf.dg_make_token(P_CLEARED, now=NOW)
        pid, exp, sig = tok.split(".")
        for bad in (f"{P_HOLD}.{exp}.{sig}", f"{pid}.{int(exp) + 1}.{sig}",
                    f"{pid}.{exp}.{('0' if sig[0] != '0' else '1') + sig[1:]}",
                    view_tok(pid, "view1", exp, secret="other-secret"), tok + "x", "", None):
            self.assertIsNone(lf.dg_verify_token(bad, now=NOW), bad)
        self.assertIsNone(lf.dg_verify_token(tok, "viewc1", now=NOW))   # wrong purpose

    def test_no_secret_disables_links(self):
        tok = lf.dg_make_token(P_CLEARED, now=NOW)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": ""}):
            self.assertIsNone(lf.dg_make_token(P_CLEARED, now=NOW))
            self.assertIsNone(lf.dg_verify_token(tok, now=NOW))
            self.assertIsNone(lf.dg_view_cookie(P_CLEARED))


class LinksAndCookies(unittest.TestCase):
    def test_valid_v_sets_cookie_and_redirects_without_v(self):
        reset(lf, switch=False)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            tok = lf.dg_make_token(P_CLEARED)
        r = run(lf, qp={"v": tok, "utm": "a b"})
        self.assertEqual(r["statusCode"], 302)
        self.assertEqual(r["headers"]["Location"], f"https://trades.graciagroup.com/deal/{DEAL_ID}?utm=a+b")
        self.assertEqual(len(r["cookies"]), 1)
        c = r["cookies"][0]
        self.assertTrue(c.startswith("gg_view="))
        for attr in ("Max-Age=15811200", "Path=/", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attr, c)
        pid = lf.dg_verify_token(c.split(";")[0].split("=", 1)[1], "viewc1")
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            self.assertEqual(lf.dg_verify_token(c.split(";")[0].split("=", 1)[1], "viewc1"), P_CLEARED)
        self.assertIsNone(pid)   # secret not in env outside the patch -> disabled

    def test_invalid_or_expired_v_renders_normally(self):
        reset(lf, switch=False)
        for tok in ("101.1.0123456789abcdef0123456789abcdef", view_tok(P_CLEARED, "view1", 1000)):
            r = run(lf, qp={"v": tok})
            self.assertEqual(r["statusCode"], 200)
            self.assertNotIn("cookies", r)

    def test_v_disabled_without_secret(self):
        reset(lf, switch=False)
        tok = view_tok(P_CLEARED, "view1", int(time.time()) + 999)
        r = run(lf, qp={"v": tok}, env={"VIEW_LINK_SECRET": ""})
        self.assertEqual(r["statusCode"], 200)

    def test_gp_sets_gg_view_alongside_gg_pd_v2_and_v1(self):
        exp = int(time.time()) + 3600
        access = {"people": {P_CLEARED: {"intermediary": True, "pd": 7218530}}}
        for v2 in (True, False):
            reset(lf, switch=True)
            S3.objects[(B, "partner-desk/access.json")] = json.dumps(access).encode()
            r = run(lf, qp={"gp": pd_tok(P_CLEARED, exp)}, v2=v2)
            if v2:
                cookies = r["cookies"]
                self.assertNotIn("Set-Cookie", r["headers"])
            else:
                self.assertNotIn("cookies", r)
                self.assertNotIn("Set-Cookie", r["headers"])
                cookies = r["multiValueHeaders"]["Set-Cookie"]
            names = sorted(c.split("=", 1)[0] for c in cookies)
            self.assertEqual(names, ["gg_pd", "gg_view"], v2)
            self.assertIn("31.17", r["body"])            # gp pid is cleared -> terms shown
            self.assertIn("pdOverlay", r["body"])        # partner desk unchanged

    def test_gp_switch_off_unchanged(self):
        exp = int(time.time()) + 3600
        reset(lf, switch=False)
        S3.objects[(B, "partner-desk/access.json")] = json.dumps(
            {"people": {P_CLEARED: {"intermediary": True, "pd": 7218530}}}).encode()
        r = run(lf, qp={"gp": pd_tok(P_CLEARED, exp)})
        self.assertEqual([c.split("=", 1)[0] for c in r["cookies"]], ["gg_pd"])

    def test_add_cookies_keeps_existing(self):
        r = lf.dg_add_cookies({"headers": {"Set-Cookie": "a=1"}}, {"path": "/x"}, ["b=2"])
        self.assertEqual(r["multiValueHeaders"]["Set-Cookie"], ["a=1", "b=2"])
        r = lf.dg_add_cookies({"cookies": ["a=1"], "headers": {}}, {"rawPath": "/x"}, ["b=2"])
        self.assertEqual(r["cookies"], ["a=1", "b=2"])


class ViewOnly(unittest.TestCase):
    """gg_view only unlocks terms: it never signs anyone in."""

    def test_gg_view_never_yields_gg_id_or_sso(self):
        exp = int(time.time()) + 3600
        reset(lf, switch=True)
        cookies = {"gg_view": view_tok(P_CLEARED, "viewc1", exp)}
        r = run(lf, cookies=cookies)
        body = r["body"]
        self.assertIn("31.17", body)                     # terms unlocked
        self.assertNotIn("sso=", body)                   # no SSO handoff anywhere
        self.assertNotIn("Signed in as", body)
        self.assertIn(">Sign In</a>", body)
        self.assertFalse([c for c in r.get("cookies", []) if c.startswith("gg_id")])
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            self.assertIsNone(lf._read_identity_email(event(None, cookies)))

    def test_redacted_log_event(self):
        e = event({"v": "secret-token", "admin_key": "k", "x": "1"}, {"gg_id": "abc"})
        e["body"] = "email=a@b.c"
        red = json.dumps(lf.dg_redacted_event(e))
        for s in ("secret-token", '"k"', "abc", "a@b.c"):
            self.assertNotIn(s, red)
        self.assertIn('"x": "1"', red)


class AdminBox(unittest.TestCase):
    def test_admin_copy_box_only_for_admins(self):
        reset(lf, switch=True)
        self.assertIn("Copy link for", run(lf, cookies={"admin_key": ADMIN})["body"])
        reset(lf, switch=True)
        self.assertNotIn("Copy link for", run(lf, cookies={"gg_id": gg_id("ann@x.com")})["body"])
        reset(lf, switch=False)
        body = run(lf)["body"]
        self.assertNotIn("Copy link for", body)
        self.assertNotIn("view-link", body)


class ClosedAndAlt(unittest.TestCase):
    def test_closed_public_page_gated(self):
        reset(lf, switch=True)
        closed = _deal()
        closed["deal_stage"] = {"id": 9, "name": "Won"}
        S3.objects[("pipeline-public-deal-data", "pipeline_deals.json")] = json.dumps([
            {"id": 77, "company": "Acme", "type": "Sell Order", "min_deal_size": 456000,
             "max_deal_size": 654000, "structure": "Fund (SPV)", "stage": "Firm"}]).encode()

        def uo(req, *a, **k):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if url.endswith("/deals/" + DEAL_ID):
                return _Resp(json.dumps(closed).encode())
            return fake_urlopen(req)
        with mock.patch.object(lf.urllib.request, "urlopen", uo), \
                mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            body = lf.lambda_handler(event(), None)["body"]
        for s in SECRET_STRINGS + ["456K", "654K", "$456", "$654"]:
            self.assertNotIn(s, body, s)
        self.assertIn("Sell · Deal 77", body)
        self.assertIn("no longer available", body)


TERM = "31.17"   # a real offer term in the fixture


# --- Hard block (Rainmaker / brokers) ---------------------------------------
SRC_KEY = "deal-gate/source-field.json"
SRC_FIELD = "custom_label_9999001"
HARD_PEOPLE = [
    {"id": 201, "first_name": "Rob", "email": "rob@rainmakersecurities.com",          # a: domain
     "custom_fields": {"custom_label_3923758": 6950563}},
    {"id": 202, "first_name": "Amy", "emails": [{"email": "amy@sg.RainmakerAPAC.com"}, {"email": "amy@y.com"}],
     "custom_fields": {"custom_label_3923758": 6950564}},                              # a: subdomain, 2nd email
    {"id": 203, "first_name": "Sta", "email": "STaylor@spry.vc",                       # b: listed email
     "custom_fields": {"custom_label_3923758": 6950563}},
    {"id": 204, "first_name": "Cob", "email": "cob@y.com",                             # c: Co-Broker
     "custom_fields": {"custom_label_3923758": 6950563, "custom_label_3759163": [6577160, 6484810]}},
    {"id": 205, "first_name": "Ffi", "email": "ffi@y.com",                             # c: Foreign Finder
     "custom_fields": {"custom_label_3923758": 7209227, "custom_label_3759163": 6888332}},
    {"id": 206, "first_name": "Int", "email": "int@y.com",                             # c: Intermediary - Other
     "custom_fields": {"custom_label_3923758": 6950563, "custom_label_3759163": [{"id": 6888333}]}},
    {"id": 207, "first_name": "Src", "email": "src@y.com",                             # Source = RMS: rule d removed
     "custom_fields": {"custom_label_3923758": 6950563, SRC_FIELD: 7300001}},
    {"id": 208, "first_name": "Txt", "email": "txt@y.com",                             # Source text "RMS": not blocked
     "custom_fields": {"custom_label_3923758": 6950563, SRC_FIELD: " rms "}},
    {"id": 209, "first_name": "Dup", "email": "dup@y.com",                             # shared: hard + cleared
     "custom_fields": {"custom_label_3923758": 6950562, "custom_label_3759163": 6888332}},
    {"id": 210, "first_name": "Dup", "email": "dup@y.com", "custom_fields": {"custom_label_3923758": 6950564}},
    {"id": 211, "first_name": "Okk", "email": "ok@notrainmakersecurities.com",         # negatives
     "custom_fields": {"custom_label_3923758": 6950563, "custom_label_3759163": 6484810, SRC_FIELD: 7300002}},
]
HARD_EMAILS = ["rob@rainmakersecurities.com", "amy@y.com", "staylor@spry.vc", "cob@y.com", "ffi@y.com",
               "int@y.com", "dup@y.com"]


def hard_reset(mod, switch=True, source=True):
    """source=True also leaves an old Source-rule file in S3: it must be ignored."""
    reset(mod, switch=switch)
    S3.objects[(B, SLIM)] = json.dumps({"people": PEOPLE + HARD_PEOPLE}).encode()
    if source:
        S3.objects[(B, SRC_KEY)] = json.dumps({"field_key": SRC_FIELD, "rms_ids": ["7300001"]}).encode()


def hard_state(cookies):
    with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
        e = event(None, cookies)
        return lf.dg_decide(e, {}, False)[:2]


class HardBlock(unittest.TestCase):
    def test_each_rule_beats_cleared_level(self):
        for em in HARD_EMAILS:
            hard_reset(lf)
            self.assertEqual(hard_state({"gg_id": gg_id(em)}), (True, "blocked"), em)
        hard_reset(lf)
        self.assertEqual(hard_state({"gg_id": gg_id("ok@notrainmakersecurities.com")}), (False, "cleared"))
        self.assertEqual(hard_state({"gg_id": gg_id("ann@x.com")}), (False, "cleared"))

    def test_rule_flags_on_records(self):
        hard_reset(lf)
        lf._people_slim_status_index()
        dg = lf._people_slim_cache["dg_by_id"]
        self.assertEqual(sorted(p for p, r in dg.items() if r[5]),
                         ["201", "202", "203", "204", "205", "206", "209"])
        self.assertEqual(lf._people_slim_cache["dg_field_counts"], {"transactor": 5})

    def test_source_rms_rule_removed(self):
        for em in ("src@y.com", "txt@y.com"):
            hard_reset(lf)                      # old source-field.json present in S3
            self.assertEqual(hard_state({"gg_id": gg_id(em)}), (False, "cleared"), em)
            self.assertNotIn(SRC_KEY, S3.gets)  # never read
        self.assertFalse(hasattr(lf, "dg_source_rule"))
        self.assertFalse(hasattr(lf, "DG_SOURCE_KEY"))

    def test_viewer_email_itself_without_record(self):
        for em in ("zoe@rainmakersecurities.com", "x@mail.rainmakerapac.com", "lh@lab-137.com"):
            hard_reset(lf)
            self.assertEqual(hard_state({"gg_id": gg_id(em)}), (True, "blocked"), em)
        hard_reset(lf)
        self.assertEqual(hard_state({"gg_id": gg_id("x@rainmakersecurities.co")}), (True, "pending"))
        reset(lf, switch=True, slim=False)          # index unreadable: hard email still blocked
        self.assertEqual(hard_state({"gg_id": gg_id("zoe@rainmakersecurities.com")}), (True, "blocked"))

    def test_hard_beats_cleared_on_combined_identity(self):
        exp = int(time.time()) + 3600
        hard_reset(lf)
        self.assertEqual(lf.dg_combine(["cleared", "hard"]), "blocked")
        self.assertEqual(lf.dg_combine(["cleared", "blocked"]), "cleared")
        for cookies in ({"gg_id": gg_id("ann@x.com"), "gg_view": view_tok("201", "viewc1", exp)},
                        {"gg_id": gg_id("zoe@rainmakersecurities.com"), "gg_view": view_tok("101", "viewc1", exp)},
                        {"gg_view": view_tok("204", "viewc1", exp)}):
            hard_reset(lf)
            self.assertEqual(hard_state(cookies), (True, "blocked"), cookies)

    def test_hard_beats_valid_view_link(self):
        hard_reset(lf)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            tok = lf.dg_make_token("204")
        r = run(lf, qp={"v": tok})
        self.assertEqual(r["statusCode"], 302)
        cookie = r["cookies"][0].split(";")[0].split("=", 1)
        hard_reset(lf)
        body = run(lf, cookies={cookie[0]: cookie[1]})["body"]
        self.assertNotIn(TERM, body)
        self.assertIn("Your access is being reviewed", body)
        for s in ("Rainmaker/broker", "Hard-blocked", "I confirm I am an accredited investor", 'name="t"'):
            self.assertNotIn(s, body)

    def test_switch_off_unchanged_for_hard_viewer(self):
        hard_reset(lf, switch=False)
        r = run(lf, cookies={"gg_id": gg_id("rob@rainmakersecurities.com")})
        self.assertIn(TERM, r["body"])
        self.assertNotIn('class="gated"', r["body"])
        self.assertFalse([k for k in S3.gets if SLIM in k or SRC_KEY in k])


# --- Self-confirm (blank / Unknown / Hold viewers; never clears) -------------
SC_OPT = 7223459
THANKS = "Thank you. Your access is being reviewed and we&#x27;ll be in touch."
QUEUE = "deal-gate/queue.json"
APPROVED = "deal-gate/approved.json"


def page(cookies=None, qp=None, env=None):
    return run(lf, qp=qp, cookies=cookies, env=env)["body"]


def form_token(body):
    return body.split('name="t" value="', 1)[1].split('"', 1)[0]


class SelfConfirmBox(unittest.TestCase):
    def test_details_form_for_signed_in_pending_viewers(self):
        for email in ("hal@x.com", "bea@x.com", "nobody@x.com"):      # Hold, blank, no CRM record
            hard_reset(lf)
            body = page({"gg_id": gg_id(email)})
            self.assertNotIn(TERM, body)
            self.assertIn("<h2>Request access to deal terms</h2>", body, email)
            self.assertIn('action="https://trades.graciagroup.com/?view=access-details"', body)
            self.assertIn('<input type="hidden" name="path" value="/deal/4242">', body)
            for f in ('name="first_name" required', 'name="last_name" required', '<select name="country" required>',
                      '<option value="Canada">Canada</option>', 'value="yes" required> I am an accredited investor',
                      "value=\"unsure\"> I'm not sure", '<button type="submit">Submit</button>'):
                self.assertIn(f, body, (email, f))
            self.assertNotIn("I confirm I am an accredited investor", body)
            with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
                self.assertEqual(lf.dg_verify_token(form_token(body), "adf1"), lf.dg_email_tag(email))

    def test_form_wording_and_linkedin_field_on_deal_page(self):
        hard_reset(lf)
        body = page({"gg_id": gg_id("hal@x.com")}).replace("&#x27;", "'")
        self.assertIn("<h2>Request access to deal terms</h2>", body)
        self.assertIn("Securities rules limit pricing to investors we've reviewed. Tell us who you are and "
                      "we'll reply within one business day.", body)
        self.assertIn('LinkedIn profile (optional)<input type="text" name="linkedin" maxlength="200" '
                      'placeholder="linkedin.com/in/yourname"', body)
        self.assertEqual(lf.dg_normalize_linkedin("linkedin.com/in/hal"), "https://www.linkedin.com/in/hal")
        self.assertEqual(lf.dg_normalize_linkedin("evil.example"), "")

    def test_no_form_for_anon_cleared_blocked_hard_or_no_secret(self):
        for cookies, env in (({}, None), ({"gg_id": gg_id("ann@x.com")}, None),
                             ({"gg_id": gg_id("ned@x.com")}, None),
                             ({"gg_id": gg_id("rob@rainmakersecurities.com")}, None),
                             ({"gg_id": gg_id("cob@y.com")}, None),
                             ({"gg_id": gg_id("sam@x.com")}, None),
                             ({"gg_id": gg_id("hal@x.com")}, {"VIEW_LINK_SECRET": ""})):
            hard_reset(lf)
            body = page(cookies, env=env)
            self.assertNotIn("Request access to deal terms", body, cookies)
            self.assertNotIn("access-details", body, cookies)

    def test_submitted_details_show_thank_you_and_terms_stay_blurred(self):
        for ans, text in (("yes", "Thanks, Hal. We'll review your details and email you within one business day."),
                          ("unsure", "Thanks, Hal. Please complete the Investor Qualification Form so we can review your access.")):
            hard_reset(lf)
            S3.objects[(B, QUEUE)] = json.dumps({"items": {"hal@x.com": {
                "email": "hal@x.com", "person_id": "102", "reason": "signup", "first_name": "Hal",
                "last_name": "Hold", "country": "Canada", "employer": "", "accredited": ans,
                "details_at": "2030-01-01T00:00:00+00:00", "from": "/deal/4242"}}, "declined": {}}).encode()
            body = page({"gg_id": gg_id("hal@x.com")})
            self.assertNotIn(TERM, body)
            self.assertIn(text, body.replace("&#x27;", "'"), ans)
            self.assertNotIn("Request access to deal terms", body)
            self.assertIn('target="_blank" rel="noopener"', body)

    def test_detail_and_token(self):
        hard_reset(lf)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            self.assertEqual(lf.dg_viewer_detail(event(None, {"gg_id": gg_id("hal@x.com")})), ("pending", ("102",), False))
            self.assertEqual(lf.dg_viewer_detail(event(None, {"gg_id": gg_id("bea@x.com")})), ("pending", ("104",), False))
            self.assertEqual(lf.dg_viewer_detail(event(None, {"gg_id": gg_id("sam@x.com")})), ("pending", (), False))
            self.assertEqual(lf.dg_viewer_detail(event(None, {"gg_id": gg_id("nobody@x.com")})), ("pending", (), False))
            self.assertEqual(lf.dg_viewer_detail(event(None, {"gg_id": gg_id("ned@x.com")})), ("blocked", (), False))
            self.assertIsNone(lf.dg_self_confirm_token(event(None, {"gg_id": gg_id("ann@x.com")})))

    def test_self_confirmed_level_stays_pending_with_thanks(self):
        self.assertEqual(lf.dg_level_state([SC_OPT]), "pending")
        hard_reset(lf)
        lf._people_slim_status_index()
        lf.dg_mark_level(["102"], SC_OPT)
        self.assertEqual(hard_state({"gg_id": gg_id("hal@x.com")}), (True, "pending"))
        body = page({"gg_id": gg_id("hal@x.com")})
        self.assertNotIn(TERM, body)
        self.assertIn(THANKS, body)
        self.assertNotIn("I confirm I am an accredited investor", body)

    def test_gg_sc_cookie_only_shows_thanks(self):
        exp = int(time.time()) + 3600
        cases = [({"gg_id": gg_id("hal@x.com"), "gg_sc": view_tok("102", "sc1", exp)}, (True, "pending"), True),
                 ({"gg_id": gg_id("hal@x.com"), "gg_sc": view_tok("101", "sc1", exp)}, (True, "pending"), False),
                 ({"gg_id": gg_id("hal@x.com"), "gg_sc": view_tok("102", "sc1", 1000)}, (True, "pending"), False),
                 ({"gg_id": gg_id("ned@x.com"), "gg_sc": view_tok("103", "sc1", exp)}, (True, "blocked"), False)]
        for cookies, want, thanks in cases:
            hard_reset(lf)
            self.assertEqual(hard_state(cookies), want, cookies)
            hard_reset(lf)
            body = page(cookies)
            self.assertNotIn(TERM, body)
            self.assertEqual(THANKS in body, thanks, cookies)

    def test_admin_preview_values(self):
        for switch in (False, True):
            hard_reset(lf, switch=switch)
            body = page({"admin_key": ADMIN}, qp={"gate_preview": "confirm"})
            self.assertIn("Request access to deal terms", body)
            self.assertIn('name="t" value=""', body)
            hard_reset(lf, switch=switch)
            body = page({"admin_key": ADMIN}, qp={"gate_preview": "confirmed"})
            self.assertIn(THANKS, body)
            self.assertNotIn("I confirm I am an accredited investor", body)
            for prev in ("pending", "blocked"):
                hard_reset(lf, switch=switch)
                self.assertNotIn("I confirm I am an accredited investor",
                                 page({"admin_key": ADMIN}, qp={"gate_preview": prev}))

    def test_deal_page_queues_signed_in_pending_viewer(self):
        hard_reset(lf)
        page({"gg_id": gg_id("nobody@x.com")})            # no CRM record
        page({"gg_id": gg_id("hal@x.com")})               # Hold record
        q = json.loads(S3.objects[(B, QUEUE)])
        self.assertEqual(set(q["items"]), {"nobody@x.com", "hal@x.com"})
        self.assertEqual((q["items"]["nobody@x.com"]["reason"], q["items"]["nobody@x.com"]["person_id"]),
                         ("signup", None))
        self.assertEqual(q["items"]["hal@x.com"]["person_id"], "102")
        n = S3.puts.count(QUEUE)
        page({"gg_id": gg_id("hal@x.com")})               # once per email
        self.assertEqual(S3.puts.count(QUEUE), n)

    def test_deal_page_does_not_queue_anon_cleared_blank_declined_or_switch_off(self):
        hard_reset(lf)
        S3.objects[(B, QUEUE)] = json.dumps({"items": {}, "declined": {"hal@x.com": "2030-01-01T00:00:00+00:00"}}).encode()
        for cookies in ({}, {"gg_id": gg_id("ann@x.com")}, {"gg_id": gg_id("bea@x.com")},
                        {"gg_id": gg_id("ned@x.com")}, {"gg_id": gg_id("rob@rainmakersecurities.com")},
                        {"gg_id": gg_id("hal@x.com")}):
            page(cookies)
        self.assertNotIn(QUEUE, S3.puts)
        hard_reset(lf, switch=False)
        page({"gg_id": gg_id("nobody@x.com")})
        self.assertNotIn(QUEUE, S3.puts)

    def test_deal_page_queue_failure_still_renders(self):
        hard_reset(lf)
        S3.deny_put = {QUEUE} if hasattr(S3, "deny_put") else set()
        S3.objects[(B, QUEUE)] = b"not json"
        with self.assertLogs(level="INFO") as logs:
            body = page({"gg_id": gg_id("nobody@x.com")})
        self.assertIn("Request access to deal terms", body)
        self.assertNotIn(TERM, body)
        joined = "\n".join(logs.output)
        self.assertIn("deal-gate: access queue write failed", joined)
        self.assertNotIn("nobody@x.com", joined)

    def test_approved_clears_unless_hard(self):
        from datetime import datetime, timezone
        now = time.time()
        hard_reset(lf)
        S3.objects[(B, APPROVED)] = json.dumps({
            "102": datetime.fromtimestamp(now - 60, timezone.utc).isoformat(),
            "104": datetime.fromtimestamp(now - 4 * 3600, timezone.utc).isoformat(),
            "201": datetime.fromtimestamp(now - 60, timezone.utc).isoformat()}).encode()
        self.assertEqual(hard_state({"gg_id": gg_id("hal@x.com")}), (False, "cleared"))
        self.assertIn(TERM, page({"gg_id": gg_id("hal@x.com")}))
        self.assertEqual(hard_state({"gg_id": gg_id("bea@x.com")})[1], "pending")      # older than 3 h
        self.assertEqual(hard_state({"gg_id": gg_id("rob@rainmakersecurities.com")})[1], "blocked")

    def test_each_level_value(self):
        want = {"6950561": "pending", "7161646": "pending", "6950562": "blocked", "7162165": "cleared",
                "7223459": "pending", "6950563": "cleared", "7209227": "cleared", "6950564": "cleared",
                "1234567": "pending"}
        for opt, st in want.items():
            self.assertEqual(lf.dg_level_state([opt]), st, opt)
            self.assertEqual(lf.dg_level_state([int(opt)]), st, opt)
        self.assertEqual(lf.dg_level_state([]), "pending")
        self.assertEqual(lf.dg_level_state(None), "pending")

    def test_switch_off_hold_viewer_unchanged(self):
        hard_reset(lf, switch=False)
        body = page({"gg_id": gg_id("hal@x.com")})
        self.assertIn(TERM, body)
        self.assertNotIn("I confirm I am an accredited investor", body)


# --- Public URL behind the Trades API Gateway proxy; six-month links; log redaction ---
SITE = "https://trades.graciagroup.com"


def gw_http_proxy_event(qs, cookies=None, headers=None):
    """What CRMDealDetails receives for https://trades.graciagroup.com/deal/{id}: API Gateway
    REST API "Trades" -> HTTP proxy -> Lambda Function URL (payload v2). The Lambda's own
    path is "/" and the deal id arrives as ?deal_id=."""
    q = "&".join(f"{k}={v}" for k, v in qs)
    e = {"version": "2.0", "routeKey": "$default", "rawPath": "/", "rawQueryString": q,
         "queryStringParameters": dict(qs),
         "headers": dict({"host": "abcdefgh.lambda-url.us-east-1.on.aws",
                          "x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https"}, **(headers or {})),
         "requestContext": {"domainName": "abcdefgh.lambda-url.us-east-1.on.aws",
                            "http": {"method": "GET", "path": "/", "protocol": "HTTP/1.1",
                                     "sourceIp": "203.0.113.7"}},
         "isBase64Encoded": False}
    if cookies:
        e["cookies"] = [f"{k}={v}" for k, v in cookies.items()]
    return e


def gw_lambda_proxy_event(deal_id, qp, headers=None):
    """REST API Lambda proxy (payload v1) for /deal/{deal_id}."""
    return {"resource": "/deal/{deal_id}", "path": f"/deal/{deal_id}", "httpMethod": "GET",
            "headers": dict(headers or {}), "multiValueHeaders": {k: [v] for k, v in (headers or {}).items()},
            "queryStringParameters": dict(qp) or None,
            "multiValueQueryStringParameters": {k: [v] for k, v in qp.items()} or None,
            "pathParameters": {"deal_id": deal_id}, "stageVariables": None,
            "requestContext": {"resourcePath": "/deal/{deal_id}", "path": f"/prod/deal/{deal_id}",
                               "httpMethod": "GET", "identity": {"sourceIp": "203.0.113.7"}},
            "body": None, "isBase64Encoded": False}


def handle(e, env=None):
    with mock.patch.object(lf.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.dict(os.environ, env if env is not None else {"VIEW_LINK_SECRET": VIEW_SECRET}):
        return lf.lambda_handler(e, None)


class PublicUrlBehindProxy(unittest.TestCase):
    def tok(self, pid=P_CLEARED):
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            return lf.dg_make_token(pid)

    def test_v_link_lands_on_the_deal_page_not_home(self):
        tok = self.tok()
        for e, want in (
                (gw_http_proxy_event([("deal_id", DEAL_ID), ("v", tok)]), f"{SITE}/deal/{DEAL_ID}"),
                (gw_http_proxy_event([("deal_id", DEAL_ID), ("v", tok), ("utm", "a b"), ("gate_preview", "anon"),
                                      ("admin_key", "zz")]), f"{SITE}/deal/{DEAL_ID}?utm=a+b"),
                (dict(gw_http_proxy_event([("deal_id", DEAL_ID), ("v", tok)]), rawPath=f"/{DEAL_ID}"),
                 f"{SITE}/deal/{DEAL_ID}"),
                (gw_lambda_proxy_event(DEAL_ID, {"v": tok}), f"{SITE}/deal/{DEAL_ID}")):
            reset(lf, switch=False)
            r = handle(e)
            self.assertEqual(r["statusCode"], 302)
            self.assertEqual(r["headers"]["Location"], want)
            c = r["cookies"][0] if "cookies" in r else r["multiValueHeaders"]["Set-Cookie"][0]
            self.assertTrue(c.startswith("gg_view="))

    def test_redirect_never_leaves_site(self):
        tok = self.tok()
        for bad in ("//evil.example", "..%2F..%2Fx", "https://evil.example/x", "4242\\\\evil"):
            reset(lf, switch=False)
            r = handle(gw_http_proxy_event([("deal_id", bad), ("v", tok)]))
            self.assertTrue(r["headers"]["Location"].startswith(SITE + "/deal/"), r["headers"]["Location"])
            self.assertNotIn("//evil", r["headers"]["Location"][len(SITE):])

    def test_gate_box_uses_public_url(self):
        reset(lf, switch=True)
        body = handle(gw_http_proxy_event([("deal_id", DEAL_ID), ("utm", "q")]))["body"]
        state = body.split("&amp;state=", 1)[1].split('"', 1)[0]
        state = urllib.parse.unquote(state)
        dest = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)).decode()
        self.assertEqual(dest, f"{SITE}/deal/{DEAL_ID}?utm=q")
        reset(lf, switch=True)
        body = handle(gw_http_proxy_event([("deal_id", DEAL_ID)], cookies={"gg_id": gg_id("hal@x.com")}))["body"]
        self.assertIn(f'name="path" value="/deal/{DEAL_ID}"', body)     # self-confirm returns here


class SixMonths(unittest.TestCase):
    def test_link_and_cookie_both_six_months(self):
        self.assertEqual(lf.DG_LINK_TTL, 183 * 86400)
        self.assertEqual(lf.DG_COOKIE_TTL, 183 * 86400)
        self.assertEqual(lf.DG_COOKIE_MAX_AGE, 183 * 86400)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            c = lf.dg_view_cookie(P_CLEARED, now=NOW)
            self.assertIn("Max-Age=15811200;", c)
            exp = int(c.split("=", 1)[1].split(";")[0].split(".")[1])
            self.assertEqual(exp - NOW, 183 * 86400)
            self.assertEqual(lf.dg_verify_token(c.split("=", 1)[1].split(";")[0], "viewc1", NOW + 182 * 86400), P_CLEARED)
            self.assertIsNone(lf.dg_verify_token(c.split("=", 1)[1].split(";")[0], "viewc1", NOW + 184 * 86400))

    def test_old_twelve_month_cookie_and_overlong_link_refused(self):
        now = int(time.time())
        reset(lf, switch=True)
        with mock.patch.dict(os.environ, {"VIEW_LINK_SECRET": VIEW_SECRET}):
            e = event(None, {"gg_view": view_tok(P_CLEARED, "viewc1", now + 365 * 86400)})
            self.assertEqual(lf.dg_decide(e, {}, False)[:2], (True, "anon"))
            e = event(None, {"gg_view": view_tok(P_CLEARED, "viewc1", now + 183 * 86400)})
            self.assertEqual(lf.dg_decide(e, {}, False)[:2], (False, "cleared"))
            self.assertIsNone(lf.dg_verify_token(view_tok(P_CLEARED, "view1", now + 200 * 86400)))
            self.assertEqual(lf.dg_verify_token(view_tok(P_CLEARED, "view1", now + 183 * 86400)), P_CLEARED)


class RefererRedaction(unittest.TestCase):
    SECRETS = ("SECRETADMIN", "QQTOK1", "QQSSO1", "QQTKN1", "QQCODE1", "QQSTATE1", "QQKEY1", "QQGP1")

    def referer(self):
        return ("https://trades.graciagroup.com/deal/9?admin_key=SECRETADMIN&company=Acme&v=QQTOK1&sso=QQSSO1"
                "&token=QQTKN1&code=QQCODE1&state=QQSTATE1&key=QQKEY1&gp=QQGP1&gate_preview=anon#frag")

    def test_redact_url(self):
        out = lf.dg_redact_url(self.referer())
        for s in self.SECRETS:
            self.assertNotIn(s, out)
        self.assertTrue(out.startswith("https://trades.graciagroup.com/deal/9?admin_key=%5Bredacted%5D&company=Acme"))
        self.assertTrue(out.endswith("#frag"))
        self.assertEqual(lf.dg_redact_url("https://trades.graciagroup.com"), "https://trades.graciagroup.com")
        self.assertEqual(lf.dg_redact_url("Mozilla/5.0"), "Mozilla/5.0")
        self.assertEqual(lf.dg_redact_url("/x?ADMIN_KEY=a"), "/x?ADMIN_KEY=%5Bredacted%5D")

    def test_request_log_has_no_secrets_from_referer(self):
        for e in (gw_http_proxy_event([("deal_id", DEAL_ID)], headers={"referer": self.referer()}),
                  gw_lambda_proxy_event(DEAL_ID, {}, headers={"Referer": self.referer()})):
            reset(lf, switch=False)
            with self.assertLogs(level="INFO") as logs:
                handle(e)
            joined = "\n".join(logs.output)
            self.assertIn("company=Acme", joined)
            for s in self.SECRETS:
                self.assertNotIn(s, joined)


if __name__ == "__main__":
    unittest.main()
