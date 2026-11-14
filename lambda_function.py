import json
import html as html_mod
import urllib.request
import urllib.error
import urllib.parse
import logging
import os
import time
import base64
import hmac
import hashlib
import re
from datetime import datetime, timedelta, timezone  # NEW: Import for date handling

API_KEY = "defe67185ffd4993874558be8c4eb29b"  # Your actual NewsAPI key

# Companies whose news search returns mostly junk -- the News section is skipped
# for these. To turn news off for a company, add its name here (case-insensitive,
# matched against the company name exactly). To turn it back on, remove the line.
NEWS_SKIP_COMPANIES = {
    "1x",
}

QUESTION_CATALOG_BUYER = [   # shown on SELL orders (a buyer asking about the seller)
    {"id": "still_active", "q": "Is this order still active?", "field": None},
    {"id": "accept_bid",   "q": "Would you accept a bid of $___/share (gross)?",                    "field": None},
    {"id": "deadline",     "q": "When is the deadline to commit?",                                  "field": None},
    {"id": "class",        "q": "Are these shares common or preferred?",                            "field": "Class"},
    {"id": "min_max",      "q": "What is the minimum / maximum size?",                              "field": "min_max"},
    {"id": "shares_avail", "q": "How many shares are available to buy?",                            "field": "Shares"},
    {"id": "max_ticket",    "q": "What is the maximum ticket you can take?", "field": "Max Deal Size"},
    {"id": "min_ticket",    "q": "What is the minimum ticket?",              "field": "Min Deal Size"},
    {"id": "est_valuation", "q": "What is the estimated valuation?",         "field": None},
    {"id": "seller_fee",   "q": "What is the seller's one-time fee?",                               "field": "Seller Fee"},
    {"id": "fee_structure","q": "Would you accept this fee structure?",                              "field": None},
    {"id": "data_room_avail","q": "Is a data room available for diligence?",                         "field": None},
    {"id": "fund_exemption", "q": "Is this a 3(c)(1) or 3(c)(7) exemption?",                         "field": None},
    {"id": "nda_l1",       "q": "Can you provide full transparency on the L1 manager under an NDA?", "field": None},
    {"id": "direct_trade", "q": "Do you have company permission to directly transfer?",              "field": None},
]
QUESTION_CATALOG_SELLER = [  # shown on BUY orders (a seller asking about the buyer)
    {"id": "still_active", "q": "Is this order still active?", "field": None},
    {"id": "accept_bid",   "q": "Would you bid $___/share (gross)?",            "field": None},
    {"id": "deadline",     "q": "How long is your buy order open?",             "field": None},
    {"id": "cash_on_hand", "q": "Do you have cash on hand?",                    "field": None},
    {"id": "qp_accredited","q": "What is your investor qualification level?",   "field": None},
    {"id": "iqf_done",     "q": "Have you completed the IQF with Rainmaker?",   "field": None},
    {"id": "on_cap_table", "q": "Are you already on the cap table?",            "field": None},
    {"id": "no_data_room", "q": "Do you need access to a data room to commit?", "field": None},
    {"id": "accept_common","q": "Would you accept common shares?",             "field": None},
    {"id": "accept_fund",  "q": "Would you accept a fund structure?",          "field": None},
]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import boto3, json

def get_jwt_from_s3():
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket="pipeline-token", Key="pipeline-jwt.json")
    data = json.loads(obj['Body'].read())
    return data['jwt']

JWT_TOKEN = get_jwt_from_s3()

# Client-facing links go through CloudFront, which routes on path prefix —
# keep the trailing slash on any path appended to this.
DESK_URL = "https://desk.graciagroup.com"

# --- "My Dashboard" button (additive, copied from chadgracia/trades) -----
IDENTITY_SECRET = os.environ.get("IDENTITY_SECRET", "")
SYNDICATE_DASH_URL = "https://ws4stw4iul75a7yx5dra2wmnq40kipav.lambda-url.us-east-1.on.aws"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
SYNDICATE_TENANTS_URL = (
    f"{SYNDICATE_DASH_URL}/?key={urllib.parse.quote(ADMIN_KEY, safe='')}&tenants=list"
    if ADMIN_KEY else ""
)
_syndicate_tenant_cache = {"emails": None}


def _get_cookie(event, name):
    """Read a cookie value from any Lambda event shape, else None."""
    for c in (event.get("cookies") or []):
        if c.startswith(name + "="):
            return c.split("=", 1)[1]

    raw_values = []
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == "cookie":
            raw_values.append(value)
    for key, values in (event.get("multiValueHeaders") or {}).items():
        if key.lower() == "cookie":
            raw_values.extend(values)

    for raw in raw_values:
        for c in raw.split(";"):
            c = c.strip()
            if c.startswith(name + "="):
                return c.split("=", 1)[1]
    return None


def _read_identity_email(event):
    """Verified email from the gg_id cookie, or None. Never raises."""
    if not IDENTITY_SECRET:
        return None
    raw = _get_cookie(event, "gg_id")
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        email, sig = decoded.rsplit("|", 1)
        expected = hmac.new(IDENTITY_SECRET.encode(), email.encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return email
    except Exception:
        return None


def _make_handoff_token(email):
    """Short-lived signed handoff the syndicate dashboard verifies:
    base64url(f"{email}|{exp}|{sig}"), sig = HMAC-SHA256(IDENTITY_SECRET, f"{email}|{exp}")."""
    exp = int(time.time()) + 3600
    sig = hmac.new(IDENTITY_SECRET.encode(), f"{email}|{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{email}|{exp}|{sig}".encode()).decode().rstrip("=")


def _syndicate_eligible_emails():
    """Lowercased emails eligible for the Syndicate Dashboard, fetched once per
    warm container from syndicate-dash's admin-gated ?tenants=list route. Short
    timeout, fail-soft: any error caches an empty set so the button just renders
    nothing rather than erroring or retrying every request."""
    if _syndicate_tenant_cache["emails"] is not None:
        return _syndicate_tenant_cache["emails"]
    emails = set()
    if not SYNDICATE_TENANTS_URL:
        # No ADMIN_KEY configured: skip the admin-gated fetch; My Dashboard is omitted.
        _syndicate_tenant_cache["emails"] = emails
        return emails
    try:
        req = urllib.request.Request(SYNDICATE_TENANTS_URL)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        emails = {(t.get("email") or "").strip().lower()
                  for t in (data.get("tenants") or []) if t.get("email")}
    except Exception as e:
        logger.warning(f"Syndicate tenants fetch failed (non-fatal): {e}")
    _syndicate_tenant_cache["emails"] = emails
    return emails


# --- Bid/Offer auction routing --------------------------------------------
# Mirrors portfolio-deploy's own auctions.json read + open/closed rule, so
# the Bid button can send a buyer straight to a live auction instead of
# web-bid. Any read/parse failure here must fall back to web-bid, never a
# broken page.
AUCTIONS_BUCKET = "full-pipeline-cache"
AUCTIONS_KEY = "auctions.json"


def _load_auctions():
    try:
        s3 = boto3.client('s3')
        obj = s3.get_object(Bucket=AUCTIONS_BUCKET, Key=AUCTIONS_KEY)
        data = json.loads(obj['Body'].read())
        return data.get('auctions') or {}
    except Exception as e:
        logger.warning(f"auctions.json load failed (non-fatal): {e}")
        return {}


def live_auction_for_deal(deal_id):
    """The id of the open auction seeded from this deal, or None. Open means
    no close_date, or a close_date of today or later (portfolio-deploy's
    own rule); deal ids are compared as str() on both sides."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        for aid, auc in (_load_auctions() or {}).items():
            if str(auc.get('deal_id') or '') != str(deal_id):
                continue
            close_date = (auc.get('close_date') or '').strip()
            if not close_date or close_date >= today:
                return aid
        return None
    except Exception as e:
        logger.warning(f"live_auction_for_deal failed (non-fatal): {e}")
        return None


def _live_auctions_for_nav():
    """[(auction_id, auction_dict), ...] for every open auction, filtered
    from the same auctions.json this file already loads for the Bid button
    (_load_auctions) -- no separate S3 read. Any failure returns [] so the
    nav's Auctions tab just doesn't render rather than breaking the page."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        live = []
        for aid, auc in (_load_auctions() or {}).items():
            close_date = (auc.get('close_date') or '').strip()
            if not close_date or close_date >= today:
                live.append((aid, auc))
        return live
    except Exception as e:
        logger.warning(f"Auctions nav tab failed (non-fatal): {e}")
        return []


def _nav_login_url(dest):
    """Cognito hosted-UI login URL carrying `dest` (bare, no token) as
    base64url state; trades' code-exchange leg appends a fresh sso token
    once the visitor signs in. Kept separate from the Bid modal's own
    login/signup URL builder so that code is left exactly as it is."""
    state = base64.urlsafe_b64encode(dest.encode()).decode().rstrip('=')
    return (
        "https://us-east-1dsttcaqx7.auth.us-east-1.amazoncognito.com/login"
        "?client_id=71vrglkidm13jb73u7nje3d1t2&response_type=code&scope=openid+email"
        "&redirect_uri=https://trades.graciagroup.com"
        f"&state={urllib.parse.quote(state, safe='')}"
    )


def _render_top_nav(event, is_admin=False, active=None):
    """The shared client-facing top nav (identical header in chadgracia/trades):
    brand, tabs (Indications, Portfolio & Watchlist, Demand Board, Auctions
    for admins when at least one is live, My Dashboard for eligible tenants), the
    deal-switcher button, and the account control on the right. Any failure
    building an optional tab must not break the rest of the nav or the page.
    `active` names the tab for the current page; it gets .nav-tab-active and
    aria-current="page". `is_admin` picks the deal-switcher's index endpoint."""
    email = _read_identity_email(event)

    if email:
        pw_token = _make_handoff_token(email)
        portfolio_href = f"{DESK_URL}/?sso={urllib.parse.quote(pw_token, safe='')}"
    else:
        portfolio_href = _nav_login_url(DESK_URL + "/")

    # Demand Board: same auth-aware link pattern as Portfolio & Watchlist above,
    # just a different destination.
    if email:
        demand_token = _make_handoff_token(email)
        demand_href = f"{DESK_URL}/?view=demand&sso={urllib.parse.quote(demand_token, safe='')}"
    else:
        demand_href = _nav_login_url(DESK_URL + "/?view=demand")

    auctions_tab = ""
    try:
        live = _live_auctions_for_nav()
        if live and is_admin:
            if len(live) == 1:
                auc_dest = f"{DESK_URL}/?view=auction&id={urllib.parse.quote(str(live[0][0]))}"
            else:
                auc_dest = f"{DESK_URL}/?view=auction_list"
            if email:
                auc_token = _make_handoff_token(email)
                auc_sep = '&' if '?' in auc_dest else '?'
                auc_href = f"{auc_dest}{auc_sep}sso={urllib.parse.quote(auc_token, safe='')}"
            else:
                auc_href = _nav_login_url(auc_dest)
            auctions_tab = (
                f'<a href="{auc_href}" class="nav-tab">'
                f'Auctions ({len(live)})</a>'
            )
    except Exception as e:
        logger.warning(f"Auctions nav tab failed (non-fatal): {e}")
        auctions_tab = ""

    dashboard_tab = ""
    try:
        if email and email.strip().lower() in _syndicate_eligible_emails():
            dash_token = _make_handoff_token(email)
            dash_href = f"{SYNDICATE_DASH_URL}/?sso={urllib.parse.quote(dash_token, safe='')}"
            dashboard_tab = f'<a href="{dash_href}" class="nav-tab">My Dashboard</a>'
    except Exception as e:
        logger.warning(f"My Dashboard nav tab failed (non-fatal): {e}")
        dashboard_tab = ""

    if email:
        safe_email = html_mod.escape(email, quote=True)
        account_html = (
            '<div class="navacct" tabindex="0">'
            '<span class="navacct-trigger">My Account &#9662;</span>'
            '<div class="navacct-menu">'
            f'<div class="navacct-item navacct-static">Signed in as {safe_email}</div>'
            '<a class="navacct-item" href="https://desk.graciagroup.com/?view=profile">Profile</a>'
            '<a class="navacct-item" href="https://trades.graciagroup.com/?signout=1">Sign out</a>'
            '</div></div>'
        )
    else:
        cur_path = (event.get('rawPath')
                    or (event.get('requestContext') or {}).get('http', {}).get('path')
                    or event.get('path') or '/')
        cur_qs = event.get('rawQueryString')
        if cur_qs is None:
            _qsp = event.get('queryStringParameters') or {}
            cur_qs = urllib.parse.urlencode(_qsp) if _qsp else ''
        cur_url = DESK_URL + cur_path + (('?' + cur_qs) if cur_qs else '')
        account_html = f'<a href="{_nav_login_url(cur_url)}" class="btn nav-signin">Sign In</a>'

    # Quick-switcher trigger (Cmd/Ctrl+K also opens it — see the
    # deal-switcher script).
    deal_switcher_btn = (
        '<button type="button" id="dealSwitcherBtn" class="nav-icon-btn" '
        'title="Switch deal (Ctrl+K)" aria-label="Switch deal">'
        '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
        '<circle cx="6.5" cy="6.5" r="4.5" fill="none" stroke="currentColor" stroke-width="1.4"></circle>'
        '<line x1="9.8" y1="9.8" x2="14" y2="14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"></line>'
        '</svg></button>'
    )

    return (
        '<nav class="topnav">'
        '<a href="https://www.graciagroup.com" class="nav-brand">Gracia Group</a>'
        '<div class="nav-tabs">'
        + ('<a href="https://trades.graciagroup.com/" class="nav-tab nav-tab-active" aria-current="page">Indications</a>'
           if active == 'indications' else
           '<a href="https://trades.graciagroup.com/" class="nav-tab">Indications</a>')
        + f'<a href="{portfolio_href}" class="nav-tab">Portfolio &amp; Watchlist</a>'
        f'<a href="{demand_href}" class="nav-tab">Demand Board</a>'
        + auctions_tab
        + dashboard_tab
        + '</div>'
        + deal_switcher_btn
        + account_html
        + '</nav>'
    )


def _render_deal_switcher_modal(is_admin=False):
    """Global 'Switch Deal' quick-switcher (same as chadgracia/trades): modal
    markup plus its script. Deal pages are served on trades.graciagroup.com,
    so the index is fetched same-origin: /?view=admin-deal-index for admins,
    /?view=public-deal-index for everyone else. Fetched once per page load
    and cached in memory; all filtering after that is client-side. This page
    has no shared copy helpers, so the script carries its own."""
    index_url = '/?view=admin-deal-index' if is_admin else '/?view=public-deal-index'
    return '''
        <div id="dealSwitcherModal" class="deal-switcher-overlay">
            <div class="deal-switcher-box">
                <input type="text" id="dealSwitcherInput" class="deal-switcher-input" placeholder="Search a company&hellip;" autocomplete="off" spellcheck="false">
                <div id="dealSwitcherResults" class="deal-switcher-results"></div>
            </div>
        </div>
        <script>
        (function () {
            var COPY_ICON_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"></rect><path d="M10.5 3.5v-1a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h1"></path></svg>';
            var CHECK_ICON_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3 8.5l3.5 3.5L13 5"></path></svg>';
            function copyTextToClipboard(text) {
                if (navigator.clipboard && window.isSecureContext) {
                    return navigator.clipboard.writeText(text);
                }
                return new Promise(function (resolve, reject) {
                    var ta = document.createElement('textarea');
                    ta.value = text;
                    ta.setAttribute('readonly', '');
                    ta.style.position = 'fixed';
                    ta.style.top = '-1000px';
                    document.body.appendChild(ta);
                    ta.select();
                    var ok = false;
                    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
                    document.body.removeChild(ta);
                    ok ? resolve() : reject(new Error('Copy failed'));
                });
            }
            var cache = null;
            var modal = document.getElementById('dealSwitcherModal');
            var input = document.getElementById('dealSwitcherInput');
            var results = document.getElementById('dealSwitcherResults');
            var btn = document.getElementById('dealSwitcherBtn');
            var rows = [];
            var idx = -1;

            function fmtSize(v) {
                if (v === null || v === undefined || v === '') return '';
                var n = Number(v);
                if (!isFinite(n)) return '';
                if (n >= 1000000) {
                    var m = Math.round((n / 1000000) * 10) / 10;
                    return '$' + (m % 1 === 0 ? m.toFixed(0) : m) + 'M';
                }
                if (n >= 1000) return '$' + Math.round(n / 1000) + 'K';
                return '$' + Math.round(n);
            }
            function fmtRange(lo, hi) {
                var a = fmtSize(lo), b = fmtSize(hi);
                if (a && b) return a === b ? a : (a + '\\u2013' + b);
                return a || b || '';
            }
            function esc(s) {
                return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
                    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
                });
            }
            function renderRows(list) {
                rows = list;
                idx = list.length ? 0 : -1;
                if (!list.length) {
                    results.innerHTML = '<div class="deal-switcher-empty">No matching deals.</div>';
                    return;
                }
                results.innerHTML = list.map(function (d, i) {
                    var label = d.status !== undefined
                        ? esc(d.company) + ' &middot; ' + esc(d.side) + ' ' + esc(fmtRange(d.size_min, d.size_max)) + ' &middot; ' + esc(d.status)
                        : [d.company, d.side, d.net,
                           d.min_size && d.max_size && d.min_size !== d.max_size ? d.min_size + '\\u2013' + d.max_size : (d.min_size || d.max_size)
                          ].filter(Boolean).map(esc).join(' &middot; ');
                    return '<div class="deal-switcher-row' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '" data-id="' + esc(d.id) + '">' +
                        '<span class="deal-switcher-label">' + label + '</span>' +
                        '<button type="button" class="deal-switcher-copy" data-id="' + esc(d.id) + '" title="Copy link" aria-label="Copy link">' + COPY_ICON_SVG + '</button>' +
                        '</div>';
                }).join('');
            }
            function setActive(newIdx) {
                var rowEls = results.querySelectorAll('.deal-switcher-row');
                if (!rowEls.length) return;
                idx = Math.max(0, Math.min(newIdx, rowEls.length - 1));
                rowEls.forEach(function (el, i) {
                    el.classList.toggle('active', i === idx);
                });
                rowEls[idx].scrollIntoView({block: 'nearest'});
            }
            function applyFilter() {
                var q = input.value.trim().toLowerCase();
                var list = !cache ? [] : (!q ? cache : cache.filter(function (d) {
                    return (d.company || '').toLowerCase().indexOf(q) !== -1;
                }));
                renderRows(list);
            }
            function goToDeal(id) {
                window.location.href = 'https://trades.graciagroup.com/deal/' + encodeURIComponent(id);
            }
            function openModal() {
                modal.classList.add('show');
                input.value = '';
                results.innerHTML = '';
                setTimeout(function () { input.focus(); }, 0);
                if (cache) {
                    renderRows(cache);
                    return;
                }
                results.innerHTML = '<div class="deal-switcher-empty">Loading&hellip;</div>';
                fetch('__INDEX_URL__', {credentials: 'same-origin'})
                    .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
                    .then(function (data) {
                        cache = (data || []).slice().sort(function (a, b) {
                            return new Date(b.updated || 0) - new Date(a.updated || 0);
                        });
                        renderRows(cache);
                    })
                    .catch(function () {
                        results.innerHTML = '<div class="deal-switcher-empty">Failed to load deals.</div>';
                    });
            }
            function closeModal() {
                modal.classList.remove('show');
            }

            if (btn) btn.addEventListener('click', openModal);
            document.addEventListener('keydown', function (e) {
                if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
                    e.preventDefault();
                    openModal();
                    return;
                }
                if (!modal.classList.contains('show')) return;
                if (e.key === 'Escape') {
                    closeModal();
                } else if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    setActive(idx + 1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    setActive(idx - 1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (idx >= 0 && rows[idx]) goToDeal(rows[idx].id);
                }
            });
            input.addEventListener('input', applyFilter);
            modal.addEventListener('click', function (e) {
                if (e.target === modal) closeModal();
            });
            results.addEventListener('click', function (e) {
                var copyBtn = e.target.closest ? e.target.closest('.deal-switcher-copy') : null;
                if (copyBtn) {
                    e.stopPropagation();
                    var id = copyBtn.getAttribute('data-id');
                    copyTextToClipboard('https://trades.graciagroup.com/deal/' + id).then(function () {
                        copyBtn.innerHTML = CHECK_ICON_SVG;
                        copyBtn.title = 'Copied';
                        clearTimeout(copyBtn._copyTimer);
                        copyBtn._copyTimer = setTimeout(function () {
                            copyBtn.innerHTML = COPY_ICON_SVG;
                            copyBtn.title = 'Copy link';
                        }, 1200);
                    });
                    return;
                }
                var row = e.target.closest ? e.target.closest('.deal-switcher-row') : null;
                if (row) goToDeal(row.getAttribute('data-id'));
            });
            results.addEventListener('mousemove', function (e) {
                var row = e.target.closest ? e.target.closest('.deal-switcher-row') : null;
                if (!row) return;
                setActive(parseInt(row.getAttribute('data-idx'), 10));
            });
        })();
        </script>
    '''.replace('__INDEX_URL__', index_url)

# --- Transferability (company multi-select) — display-only, read-only -------
# Rules driven by the Pipeline COMPANY field labelled exactly "Transferability".
# Nothing here writes to Pipeline or changes deal stages. Every failure path
# fails OPEN (no restrictions) so a page never breaks because of this lookup.
#
# Field + option ids are resolved at runtime: one read-only GET of
# /admin/company_custom_field_labels.json (JWT auth), cached in
# s3://full-pipeline-cache/transferability-field.json for 24 hours. On lookup
# failure the cached file is used even if stale; with no cached file at all,
# no restrictions apply. Company values come from full-pipeline-cache/
# companies.json and deal fields from full-pipeline-cache/deals.json (both
# crm-snapshot, hourly).
TR_FIELD_LABEL = "Transferability"
TR_DIRECT_ALLOWED = "Direct Allowed"
TR_ONLY_PREFERRED = "Only Preferred"
TR_DIRECT_BLOCKED = "Direct Blocked"
TR_APPROVED_TRANSACTORS = "Only Approved Transactors"
TR_CURRENT_CAP_TABLE = "Only Current Cap Table"
TR_OPTION_LABELS = (TR_DIRECT_ALLOWED, TR_ONLY_PREFERRED, TR_DIRECT_BLOCKED,
                    TR_APPROVED_TRANSACTORS, TR_CURRENT_CAP_TABLE)

TR_DEAL_TYPE_FIELD = "custom_label_1958"       # Deal Type
TR_DEAL_TYPE_SELL = 5011675                    # Sell
TR_STRUCTURE_FIELD = "custom_label_3064360"    # Structure
TR_STRUCTURE_DIRECT = 6250090                  # Direct
TR_SHARE_CLASS_FIELD = "custom_label_3064330"  # Share Class
TR_SHARE_CLASS_COMMON = 5077831                # Common

TR_BUCKET = "full-pipeline-cache"
TR_FIELD_CACHE_KEY = "transferability-field.json"
TR_COMPANIES_KEY = "companies.json"
TR_DEALS_KEY = "deals.json"
TR_FIELD_TTL = 24 * 3600        # field/option id resolution
TR_FIELD_RETRY_SECS = 600       # after a failed resolution, retry at most this often
TR_DATA_TTL = 600               # companies.json / deals.json per warm container

TR_UNAVAILABLE_STATUS = "Not currently available — issuer transfer restrictions"
TR_NOTE_PREFERRED = "Issuer currently allows transfers of preferred shares only."
TR_NOTE_APPROVAL = "Transfers require company approval."
TR_NOTE_CAP_TABLE = "Buyer must be an existing shareholder."

_TR_FIELD_CACHE = {'ts': 0.0, 'val': None, 'source': None}
_TR_COMPANIES_CACHE = {'ts': 0.0, 'val': None}
_TR_UNAVAILABLE_CACHE = {'ts': 0.0, 'val': None}
_TR_DEALS_CACHE = {'ts': 0.0, 'val': None}

# --- Deal Visibility ------------------------------------------------------
# Pipeline dropdown on the deal. Blank/unknown = All Investors (no change).
# Private: never shown to counterparties (non-admin deal page shows only a
# "not currently available" stub). Interested investors only: page still opens
# by direct link but is noindex. Both are dropped from every other-deal list
# (tr_filter_deals). List filtering reads deals.json plus the short-lived
# visibility-overrides.json written by deal-update-form (override wins).
VISIBILITY_FIELD = "custom_label_4013323"
VIS_ALL = 7223456          # "All Investors"
VIS_INTERESTED = 7223457   # "Interested investors only"
VIS_PRIVATE = 7223458      # "Private"
VIS_OVERRIDES_KEY = "visibility-overrides.json"   # in TR_BUCKET
VIS_OVERRIDES_TTL = 60
_VIS_OVERRIDES_CACHE = {'ts': 0.0, 'val': None}


def _tr_ids(value):
    """Normalise a (multi-)select custom field value to a list of ints.
    Handles int, numeric str, comma-separated str and lists of either."""
    if value is None or value == '':
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(',')
    out = []
    for v in items:
        try:
            out.append(int(float(str(v).strip())))
        except (TypeError, ValueError):
            continue
    return out


def _tr_parse_field_labels(data):
    """Pipeline company_custom_field_labels payload -> resolution dict, or None
    if no field is labelled exactly TR_FIELD_LABEL."""
    if isinstance(data, dict):
        entries = data.get('entries')
        if entries is None:
            entries = [data.get('entry') or data]
    else:
        entries = data or []
    for e in entries:
        if not isinstance(e, dict) or (e.get('name') or '').strip() != TR_FIELD_LABEL:
            continue
        options = {}
        for o in e.get('custom_field_label_dropdown_entries') or []:
            try:
                options[(o.get('name') or '').strip()] = int(o['id'])
            except (KeyError, TypeError, ValueError):
                continue
        fid = int(e['id'])
        return {'field_id': fid, 'field_key': f"custom_label_{fid}", 'options': options}
    return None


def _tr_pipeline_get(path):
    req = urllib.request.Request(
        "https://api.pipelinecrm.com/api/v3" + path,
        headers={"Accept": "application/json", **_tr_auth_headers()})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode())


def _tr_lookup_field_from_pipeline():
    """ONE read-only GET of the company custom field labels (a second read-only
    GET of the single label only if the list omits its dropdown options)."""
    res = _tr_parse_field_labels(
        _tr_pipeline_get("/admin/company_custom_field_labels.json?per_page=200"))
    if res is None:
        raise LookupError(f'no company custom field labelled "{TR_FIELD_LABEL}"')
    if not res['options']:
        one = _tr_parse_field_labels(
            _tr_pipeline_get(f"/admin/company_custom_field_labels/{res['field_id']}.json"))
        if one:
            res = one
    return res


def _tr_read_field_cache(s3):
    try:
        obj = s3.get_object(Bucket=TR_BUCKET, Key=TR_FIELD_CACHE_KEY)
        data = json.loads(obj['Body'].read().decode('utf-8'))
        if data.get('field_key') and isinstance(data.get('options'), dict):
            return data
    except Exception as e:
        logger.info(f"transferability: no usable S3 field cache ({e})")
    return None


def transferability_field(s3=None, now=None):
    """{'field_id', 'field_key', 'options': {label: id}, 'resolved_at'} or None.
    None means "apply no restrictions". Never raises."""
    now = time.time() if now is None else now
    c = _TR_FIELD_CACHE
    ttl = TR_FIELD_TTL if c['val'] else TR_FIELD_RETRY_SECS
    if c['ts'] and now - c['ts'] < ttl:
        return c['val']
    try:
        s3 = s3 or boto3.client('s3')
    except Exception as e:
        logger.error(f"transferability: S3 client failed: {e}")
        s3 = None
    cached = _tr_read_field_cache(s3) if s3 else None
    if cached and now - float(cached.get('resolved_at') or 0) < TR_FIELD_TTL:
        c.update(ts=now, val=cached, source='s3-cache')
        return cached
    try:
        res = _tr_lookup_field_from_pipeline()
        res['resolved_at'] = now
        missing = [l for l in TR_OPTION_LABELS if l not in res['options']]
        if missing:
            logger.warning(f"transferability: options not found on field: {missing}")
        logger.info(f"transferability: resolved {res['field_key']} options={res['options']}")
        if s3:
            try:
                s3.put_object(Bucket=TR_BUCKET, Key=TR_FIELD_CACHE_KEY,
                              Body=json.dumps(res).encode('utf-8'),
                              ContentType='application/json')
            except Exception as e:
                logger.warning(f"transferability: could not write S3 field cache: {e}")
        c.update(ts=now, val=res, source='pipeline')
        return res
    except Exception as e:
        if cached:
            logger.error(f"transferability: Pipeline lookup failed, using stale S3 cache: {e}")
            c.update(ts=now, val=cached, source='s3-cache-stale')
            return cached
        logger.error(f"transferability: lookup failed and no S3 cache; NO restrictions applied: {e}")
        c.update(ts=now, val=None, source='none')
        return None


def transferability_labels(custom_fields, field):
    """Set of option labels set on a company's custom_fields."""
    if not field or not custom_fields:
        return set()
    by_id = {v: k for k, v in (field.get('options') or {}).items()}
    return {by_id[i] for i in _tr_ids(custom_fields.get(field['field_key'])) if i in by_id}


def tr_is_direct_sell(deal_cf):
    deal_cf = deal_cf or {}
    return (TR_DEAL_TYPE_SELL in _tr_ids(deal_cf.get(TR_DEAL_TYPE_FIELD))
            and TR_STRUCTURE_DIRECT in _tr_ids(deal_cf.get(TR_STRUCTURE_FIELD)))


def tr_deal_unavailable(deal_cf, labels):
    """True if the deal must be shown as UNAVAILABLE. Buy orders never are."""
    if not labels or not tr_is_direct_sell(deal_cf):
        return False
    if TR_DIRECT_BLOCKED in labels:
        return True
    if (TR_ONLY_PREFERRED in labels
            and TR_SHARE_CLASS_COMMON in _tr_ids((deal_cf or {}).get(TR_SHARE_CLASS_FIELD))):
        return True
    return False


def tr_deal_notes(is_buy, labels):
    """Display notes for a deal whose company carries `labels`."""
    labels = labels or set()
    notes = []
    if is_buy and TR_ONLY_PREFERRED in labels:
        notes.append(TR_NOTE_PREFERRED)
    if TR_APPROVED_TRANSACTORS in labels:
        notes.append(TR_NOTE_APPROVAL)
    if TR_CURRENT_CAP_TABLE in labels:
        notes.append(TR_NOTE_CAP_TABLE)
    return notes


def _tr_s3_json(s3, key):
    obj = s3.get_object(Bucket=TR_BUCKET, Key=key)
    return json.loads(obj['Body'].read().decode('utf-8'))


def _tr_deal_company_id(d):
    cid = d.get('company_id')
    if cid in (None, '') and isinstance(d.get('company'), dict):
        cid = d['company'].get('id')
    try:
        return int(cid)
    except (TypeError, ValueError):
        return None


def transferability_companies(field, s3=None, now=None):
    """{company_id(int): {'name', 'labels': set}} for companies with any
    Transferability value, from companies.json. {} on any failure."""
    if not field:
        return {}
    now = time.time() if now is None else now
    c = _TR_COMPANIES_CACHE
    if c['val'] is not None and now - c['ts'] < TR_DATA_TTL:
        return c['val']
    out = {}
    try:
        raw = _tr_s3_json(s3 or boto3.client('s3'), TR_COMPANIES_KEY)
        recs = raw.get('companies') if isinstance(raw, dict) else raw
        for rec in recs or []:
            try:
                labels = transferability_labels(rec.get('custom_fields') or {}, field)
                if labels:
                    out[int(rec['id'])] = {'name': rec.get('name'), 'labels': labels}
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    except Exception as e:
        logger.error(f"transferability: companies.json load failed; no restrictions: {e}")
        return c['val'] or {}
    c.update(ts=now, val=out)
    return out


def tr_unavailable_ids_from_deals(deals, companies):
    """Set of deal ids (str) that are UNAVAILABLE, given raw Pipeline deals."""
    out = set()
    if not companies:
        return out
    for d in deals or []:
        try:
            co = companies.get(_tr_deal_company_id(d))
            if co and tr_deal_unavailable(d.get('custom_fields') or {}, co['labels']):
                out.add(str(d['id']))
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def _tr_deals_json(s3=None, now=None):
    """Raw Pipeline deals list from full-pipeline-cache/deals.json, cached
    TR_DATA_TTL per warm container (shared by Transferability and Visibility).
    Raises on failure; callers fail soft."""
    now = time.time() if now is None else now
    c = _TR_DEALS_CACHE
    if c['val'] is not None and now - c['ts'] < TR_DATA_TTL:
        return c['val']
    raw = _tr_s3_json(s3 or boto3.client('s3'), TR_DEALS_KEY)
    deals = raw.get('deals') if isinstance(raw, dict) else raw
    deals = deals if isinstance(deals, list) else []
    c.update(ts=now, val=deals)
    return deals


def transferability_unavailable_ids(s3=None, now=None):
    """Set of deal ids (str) to hide from lists. Empty set on any failure."""
    now = time.time() if now is None else now
    c = _TR_UNAVAILABLE_CACHE
    if c['val'] is not None and now - c['ts'] < TR_DATA_TTL:
        return c['val']
    try:
        s3 = s3 or boto3.client('s3')
        field = transferability_field(s3=s3, now=now)
        companies = transferability_companies(field, s3=s3, now=now)
        if not companies:
            out = set()
        else:
            out = tr_unavailable_ids_from_deals(_tr_deals_json(s3, now), companies)
    except Exception as e:
        logger.error(f"transferability: unavailable-deal scan failed; no restrictions: {e}")
        return c['val'] or set()
    c.update(ts=now, val=out)
    return out


def vis_value(v):
    """Normalise a Visibility value (int, numeric str, "7223458.0", 1-element
    list) to an option id; blank/unparseable/unknown -> VIS_ALL."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None or isinstance(v, bool):
        return VIS_ALL
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError, OverflowError):
        return VIS_ALL
    return n if n in (VIS_INTERESTED, VIS_PRIVATE) else VIS_ALL


def deal_visibility(custom_fields):
    """Visibility option id from a deal's custom_fields (VIS_ALL when unset)."""
    if not isinstance(custom_fields, dict):
        return VIS_ALL
    return vis_value(custom_fields.get(VISIBILITY_FIELD))


def _is_no_such_key(e):
    code = ((getattr(e, 'response', None) or {}).get('Error') or {}).get('Code')
    return code in ('NoSuchKey', '404') or type(e).__name__ == 'NoSuchKey'


def visibility_overrides(s3=None, now=None):
    """{str(deal_id): option_id} from visibility-overrides.json, cached
    VIS_OVERRIDES_TTL. Missing file = {}. Raises on other failures."""
    now = time.time() if now is None else now
    c = _VIS_OVERRIDES_CACHE
    if c['val'] is not None and now - c['ts'] < VIS_OVERRIDES_TTL:
        return c['val']
    try:
        raw = _tr_s3_json(s3 or boto3.client('s3'), VIS_OVERRIDES_KEY)
    except Exception as e:
        if not _is_no_such_key(e):
            raise
        raw = {}
    out = {}
    if isinstance(raw, dict):
        for k, rec in raw.items():
            if isinstance(rec, dict) and 'v' in rec:
                out[str(k).strip()] = vis_value(rec.get('v'))
    c.update(ts=now, val=out)
    return out


def visibility_map(deals, overrides):
    """{str(deal_id): option_id} from raw Pipeline deals, overrides on top."""
    out = {}
    for d in deals or []:
        if isinstance(d, dict) and d.get('id') is not None:
            out[str(d['id'])] = deal_visibility(d.get('custom_fields'))
    for k, v in (overrides or {}).items():
        out[str(k)] = vis_value(v)
    return out


def visibility_hidden_ids(s3=None, now=None):
    """Deal ids (str) that are Private or Interested-only: kept out of every
    other-deal list. Raises when deals.json cannot be read."""
    s3 = s3 or boto3.client('s3')
    deals = _tr_deals_json(s3, now)
    try:
        overrides = visibility_overrides(s3, now)
    except Exception as e:
        logger.error(f"visibility: overrides unreadable; using deals.json only: {type(e).__name__}")
        overrides = {}
    vmap = visibility_map(deals, overrides)
    return {k for k, v in vmap.items() if v in (VIS_PRIVATE, VIS_INTERESTED)}


def vis_filter_deals(deals):
    """Drop Private / Interested-only deals from a list. Never raises; on a
    lookup failure logs one line and returns the list unfiltered."""
    try:
        hidden = visibility_hidden_ids()
    except Exception as e:
        logger.error(f"visibility: lookup failed; no visibility filtering: {type(e).__name__}")
        return deals
    if not hidden or not isinstance(deals, list):
        return deals
    return [d for d in deals if not (isinstance(d, dict) and str(d.get('id')) in hidden)]


def tr_filter_deals(deals):
    """Drop UNAVAILABLE deals (Transferability) and Private / Interested-only
    deals (Visibility) from a pipeline_deals.json-style list. Never raises."""
    deals = vis_filter_deals(deals)
    try:
        hidden = transferability_unavailable_ids()
    except Exception as e:
        logger.error(f"transferability: filter failed; no restrictions: {e}")
        return deals
    if not hidden:
        return deals
    return [d for d in deals if str(d.get('id')) not in hidden]


def _tr_auth_headers():
    """Pipeline auth for the Transferability label lookup (module JWT)."""
    return {"Authorization": f"Bearer {JWT_TOKEN}"}


def tr_page_state(deal_cf, company_cf, is_buy):
    """(unavailable, notes) for the deal page. Fails open: (False, [])."""
    try:
        field = transferability_field()
        if not field:
            return False, []
        labels = transferability_labels(company_cf or {}, field)
        return tr_deal_unavailable(deal_cf or {}, labels), tr_deal_notes(is_buy, labels)
    except Exception as e:
        logger.error(f"transferability: deal page check failed; no restrictions: {e}")
        return False, []


def render_tr_unavailable_status():
    return ('<span class="deal-status">&bull; Status: '
            f'<span class="status-obsolete">{TR_UNAVAILABLE_STATUS}</span></span>')


def render_tr_notes(notes):
    if not notes:
        return ''
    return ('<div class="transfer-note" style="margin:8px 0 0;'
            'color:var(--text-secondary,#555);font-size:0.95em;">'
            + _sim_esc(' '.join(notes)) + '</div>')


# --- Explore Similar Companies -------------------------------------------
SIMILAR_TRADES_BASE = "https://trades.graciagroup.com/"
SIMILAR_MAX_PILLS = 12
SIMILAR_GENERIC_SHARE = 0.25
SIMILAR_RELAX_BELOW = 2
SIMILAR_WEAK_TAGS = {'saas', 'b2b', 'b2c', 'it', 'technology', 'apps', 'internet'}


def _sim_esc(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _sim_tags(value):
    return [t.strip() for t in (value or '').split(',') if t.strip()]


def _sim_load_deals():
    try:
        s3c = boto3.client('s3')
        obj = s3c.get_object(Bucket='pipeline-public-deal-data', Key='pipeline_deals.json')
        return tr_filter_deals(json.loads(obj['Body'].read().decode('utf-8')))
    except Exception as e:
        logger.warning("similar-companies: could not load pipeline_deals.json: %s", e)
        return []


def compute_similar_companies(company_name, deal_type):
    deals = _sim_load_deals()
    if not deals or not company_name:
        return [], ''

    want_type = 'Buy Order' if deal_type == 'Buy Order' else 'Sell Order'
    side_param = 'bid' if want_type == 'Buy Order' else 'offer'

    tag_map = {}
    for d in deals:
        co = d.get('company')
        if co and co not in tag_map:
            tag_map[co] = _sim_tags(d.get('company_industry'))

    freq = {}
    for tl in tag_map.values():
        for t in tl:
            freq[t] = freq.get(t, 0) + 1

    universe = len(tag_map)
    if universe == 0:
        return [], side_param
    generic = set(t for t, c in freq.items()
                  if c > SIMILAR_GENERIC_SHARE * universe or t.lower() in SIMILAR_WEAK_TAGS)

    target = set(tag_map.get(company_name) or [])
    if not target:
        return [], side_param

    eligible = {}
    for d in deals:
        co = d.get('company')
        if not co or co == company_name or d.get('type') != want_type:
            continue
        rec = eligible.get(co)
        if rec is None:
            rec = {'hi': False, 'up': ''}
            eligible[co] = rec
        if d.get('highlighted') == 'Yes':
            rec['hi'] = True
        u = str(d.get('updated') or '')
        if u > rec['up']:
            rec['up'] = u

    def build(strict):
        rows = []
        for co, meta in eligible.items():
            shared = target & set(tag_map.get(co) or [])
            if not shared:
                continue
            if strict and shared.issubset(generic):
                continue
            score = 0.0
            for t in shared:
                score += 1.0 / freq[t]
            if meta['hi']:
                score *= 1.25
            rows.append([score, meta['up'], co])
        rows.sort(key=lambda r: r[2])
        rows.sort(key=lambda r: r[1], reverse=True)
        rows.sort(key=lambda r: r[0], reverse=True)
        return rows

    ranked = build(True)
    if len(ranked) < SIMILAR_RELAX_BELOW:
        ranked = build(False)
    return [r[2] for r in ranked[:SIMILAR_MAX_PILLS]], side_param


def _sim_num(v):
    try:
        if v is None:
            return None
        s = str(v).replace('$', '').replace(',', '').replace('%', '').strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _sim_span(s_lo, s_hi, c_lo, c_hi):
    """How much of the source ticket range the candidate can absorb."""
    if s_lo is None or s_hi is None or c_lo is None or c_hi is None:
        return 0.5
    lo = max(s_lo, c_lo)
    hi = min(s_hi, c_hi)
    if hi <= lo:
        return -1.0
    span = s_hi - s_lo
    if span <= 0:
        return 1.0
    return min(1.0, (hi - lo) / span)


def _sim_rank_map(items, key_fn, reverse):
    """Rank-normalise to 0..1; entries with no value are left out."""
    known = [i for i, it in enumerate(items) if key_fn(it) is not None]
    known.sort(key=lambda i: key_fn(items[i]), reverse=reverse)
    out = {}
    n = len(known)
    for pos, i in enumerate(known):
        out[i] = 1.0 if n < 2 else 1.0 - (pos / float(n - 1))
    return out


def _sim_price(d):
    p = _sim_num(d.get('net'))
    if p is None:
        p = _sim_num(d.get('gross'))
    return p


def _sim_pick_deal(source, cands, side_param):
    """Score a company's candidate deals against the deal being viewed."""
    if not cands:
        return None, 0
    if len(cands) == 1:
        return cands[0].get('id'), 1

    src = source or {}
    s_lo = _sim_num(src.get('min_deal_size'))
    s_hi = _sim_num(src.get('max_deal_size'))
    s_struct = (src.get('structure') or '').strip().lower()
    s_layers = (src.get('layers') or '').strip().lower()
    s_mgmt = _sim_num(src.get('management_fee'))
    s_carry = _sim_num(src.get('carry'))

    price_rank = _sim_rank_map(cands, _sim_price, side_param != 'offer')
    rec_rank = _sim_rank_map(cands, lambda d: (str(d.get('updated')) if d.get('updated') else None), True)

    best_id = None
    best_score = None
    for idx, c in enumerate(cands):
        c_struct = (c.get('structure') or '').strip().lower()
        c_layers = (c.get('layers') or '').strip().lower()

        score = 3.0 * _sim_span(s_lo, s_hi,
                                _sim_num(c.get('min_deal_size')),
                                _sim_num(c.get('max_deal_size')))
        score += 3.0 * (1.0 if (s_struct and c_struct == s_struct) else 0.0)

        if s_struct == 'fund' and c_struct == 'fund':
            score += 1.5 * (1.0 if (s_layers and c_layers == s_layers) else 0.0)
        elif s_struct and c_struct and s_struct != 'fund' and c_struct != 'fund':
            score += 0.75

        score += 0.5 * (1.0 if str(c.get('data_room') or '').strip().lower() == 'yes' else 0.0)

        c_mgmt = _sim_num(c.get('management_fee'))
        c_carry = _sim_num(c.get('carry'))
        if None not in (s_mgmt, s_carry, c_mgmt, c_carry):
            gap = abs(s_mgmt - c_mgmt) + abs(s_carry - c_carry)
            score += 0.5 * max(0.0, 1.0 - (gap / 25.0))
        else:
            score += 0.25

        score += 1.0 * price_rank.get(idx, 0.5)
        score += 1.0 * rec_rank.get(idx, 0.5)

        if best_score is None or score > best_score:
            best_score = score
            best_id = c.get('id')

    return best_id, len(cands)


def render_similar_companies(company_name, deal_type, deal_id):
    try:
        names, side_param = compute_similar_companies(company_name, deal_type)
    except Exception as e:
        logger.warning("similar-companies: ranking failed: %s", e)
        return ''
    if not names:
        return ''

    deals = _sim_load_deals()
    want_type = 'Buy Order' if deal_type == 'Buy Order' else 'Sell Order'

    source = None
    for d in deals:
        if str(d.get('id')) == str(deal_id):
            source = d
            break

    by_company = {}
    for d in deals:
        co = d.get('company')
        if co and d.get('type') == want_type:
            by_company.setdefault(co, []).append(d)

    pills = ''
    for co in names:
        best_id, count = _sim_pick_deal(source, by_company.get(co) or [], side_param)
        if best_id is None:
            continue
        href = SIMILAR_TRADES_BASE + 'deal/' + urllib.parse.quote(str(best_id))
        pills += ('<a class="similar-pill" href="' + href + '">' + _sim_esc(co)
                  + '<sup class="similar-count">' + str(count) + '</sup></a>')

    if not pills:
        return ''
    label = 'offers' if side_param == 'offer' else 'bids'
    return ('<aside class="similar-box">'
            '<h2>Explore Similar Companies</h2>'
            '<div class="similar-pills">' + pills + '</div>'
            '<p class="similar-note">Closest matching live ' + label + ' by size, structure and price. '
            'Superscript shows how many are available.</p>'
            '</aside>')

def fetch_deal_data(deal_id):
    base_url = "https://api.pipelinecrm.com/api/v3"
    jwt_token = JWT_TOKEN

    deal_url = f"{base_url}/deals/{deal_id}"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(deal_url, headers=headers)
    
    try:
        with urllib.request.urlopen(req) as response:
            deal_data = json.loads(response.read().decode())
            
            # Fetch company data if company_id is available
            if 'company_id' in deal_data:
                company_data = fetch_company_data(deal_data['company_id'])
                if company_data:
                    deal_data['company_data'] = company_data
            
            return deal_data
    except urllib.error.URLError as e:
        logger.error(f"Error fetching data: {e}")
        return None

def calculate_price_comparison(price, last_round_price):
    """Calculate percentage difference from last round price"""
    try:
        if not price or not last_round_price:
            return None
        price = float(price)
        last_round_price = float(last_round_price)
        pct_diff = ((price - last_round_price) / last_round_price) * 100
        return pct_diff
    except (ValueError, TypeError, ZeroDivisionError):
        return None

def format_date(date_string):
    from datetime import datetime
    try:
        dt = datetime.strptime(date_string, '%Y/%m/%d %H:%M:%S %z')
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return date_string

def fetch_company_data(company_id):
    base_url = "https://api.pipelinecrm.com/api/v3"
    jwt_token = JWT_TOKEN
    company_url = f"{base_url}/companies/{company_id}"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(company_url, headers=headers)
    
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError as e:
        logger.error(f"Error fetching company data: {e}")
        return None

def calculate_estimated_valuation(gross_price, shares, company_lr_val, company_lr_pps):
    """
    Calculate estimated valuation based on the new gross price
    Returns valuation in billions
    """
    try:
        if not gross_price or not company_lr_val or not company_lr_pps:
            return None
            
        gross_price = float(gross_price)
        company_lr_val = float(company_lr_val)
        company_lr_pps = float(company_lr_pps)
        
        # Calculate the ratio between new and old price
        price_ratio = gross_price / company_lr_pps
        
        # Apply ratio to previous valuation
        new_valuation = company_lr_val * price_ratio
        
        return new_valuation
        
    except (ValueError, TypeError, ZeroDivisionError):
        return None

def format_price_with_comparison(price, valuation, comparison_pct):
    """Format price with valuation and comparison percentage"""
    if comparison_pct is None:
        return f"{format_currency(price, include_cents=True)}{format_valuation(valuation)}"
        
    color = '#1f7a4d' if comparison_pct >= 0 else '#b23b3b'  # Gracia pos / neg
    arrow = '↑' if comparison_pct >= 0 else '↓'
    return f'{format_currency(price, include_cents=True)}{format_valuation(valuation)} <span style="color: {color}">({comparison_pct:+.0f}% {arrow})</span>'

def format_valuation(valuation):
    """
    Format valuation in billions with one decimal place
    """
    if valuation is None:
        return ""
    return f" (${valuation:.1f}Bn)"

def format_tilde_valuation(billions):
    """Billions -> "~$4.5B" (>= 1B, one decimal max, zeros trimmed) or "~$750M"."""
    if billions >= 1 or round(billions * 1000) >= 1000:
        return "~$" + f"{billions:.1f}".rstrip("0").rstrip(".") + "B"
    return f"~${round(billions * 1000):,.0f}M"

def format_currency(value, include_cents=False):
    try:
        float_value = float(value)
        if include_cents:
            return f"${float_value:,.2f}"
        else:
            return f"${int(float_value):,}"
    except (ValueError, TypeError):
        return value

def format_percentage(value):
    try:
        return f"{float(value)}%"
    except (ValueError, TypeError):
        return value

def map_custom_fields(custom_fields):
    """Map custom fields from PipelineCRM to their human-readable names"""
    field_mapping = {
        'custom_label_3070843': 'Shares',
        'custom_label_3940558': 'Management Fee',
        'custom_label_3940559': 'Carry',
        'custom_label_3940560': 'Seller Fee',
        'custom_label_3940561': 'Partner Fee',
        'custom_label_3938743': 'Layers',
        'custom_label_3938751': 'GP SEC Registration',
        'custom_label_3938752': 'SPV Jurisdiction',
        'custom_label_3938754': 'SPVs Managed',
        'custom_label_4006089': 'Fund Exemption',
        'custom_label_3065488': 'Min Deal Size',
        'custom_label_3064645': 'Max Deal Size',
        'custom_label_3064363': 'Company LR (PPS)',
        'custom_label_3790429': 'Company LR Val ($Bn)',
        'custom_label_3064330': 'Class',
        'custom_label_3064360': 'Structure',
        'custom_label_4001285': 'Messaging',
        'custom_label_3938748': 'Seller Type',
        'custom_label_3938750': 'Price Status',
        'custom_label_3064357': 'Private Notes',
        'summary': 'Notes',
        'custom_label_1958': 'Type',
        'custom_label_3064369': 'Net',
        'custom_label_3064339': 'Gross'
    }
    
    mapped_fields = {}
    for key, value in custom_fields.items():
        if key in field_mapping:
            if key == 'custom_label_1958':  # Special handling for Type
                mapped_fields[field_mapping[key]] = value if isinstance(value, list) else [value]
            else:
                mapped_fields[field_mapping[key]] = value
    logger.info(f"Mapped custom fields: {json.dumps(mapped_fields)}")
    return mapped_fields

def get_structure_description(structures):
    if not structures:
        return ""
        
    structure_map = {
        "Direct": "Direct Transfer",
        "Fund": "Special Purpose Vehicle",
        "Forward": "Forward Contract"
    }
    
    # Split if it's a comma-separated string
    if isinstance(structures, str):
        structures = [s.strip() for s in structures.split(',')]
    elif not isinstance(structures, list):
        structures = [structures]
        
    descriptions = [structure_map.get(s, '') for s in structures if s in structure_map]
    
    # Join with ' or ' to indicate multiple options
    if descriptions:
        return f" - {' or '.join(descriptions)}"
    return ""

SEO_SERIES_OPTS = {"5077840": "Seed", "5077843": "A", "5077846": "B", "5077849": "C",
                   "5077852": "D", "5077855": "E", "5077858": "F", "5077861": "G",
                   "5077864": "H", "5077867": "I", "6539216": "N"}
SEO_SKIP_COMPANIES = {"anthropic"}
SEO_NAME_OVERRIDES = {"1x": "1X Technologies"}

def _seo_company(deal_name, company_name):
    """Cleaned company name for SEO text, or None for skipped/empty companies."""
    import re as _re
    raw = str(company_name or '')
    if not raw.strip() or raw.strip().lower() == 'unknown company':
        raw = str(deal_name or '').split(':')[0]
    comp = _re.sub(r'\s*\(.*?\)\s*', ' ', raw)
    comp = _re.sub(r'\s+', ' ', comp).strip()
    low = comp.lower()
    if any(s in low for s in SEO_SKIP_COMPANIES) or any(s in str(deal_name or '').lower() for s in SEO_SKIP_COMPANIES):
        return None
    for key, val in SEO_NAME_OVERRIDES.items():
        if low == key or low.startswith(key + ' '):
            comp = val
            break
    return comp or None

def _seo_series_text(series_raw):
    # Series from deal field custom_label_3064333 (int, str, or list)
    sv = series_raw
    if isinstance(sv, list):
        sv = sv[0] if sv else ''
    ser = SEO_SERIES_OPTS.get(str(sv).strip(), '') if sv not in (None, '') else ''
    return '' if not ser else ('Seed' if ser == 'Seed' else f'Series {ser}')

def build_seo_title(deal_name, company_name, deal_type, structure, series_raw):
    """Return the plain-text <title>, or None for skipped companies
    (caller then keeps the original deal_name title)."""
    import re as _re
    comp = _seo_company(deal_name, company_name)
    if comp is None:
        return None
    if deal_type == 'Buy Order':
        return f'Sell {comp} Pre-IPO Stock – Active Buyer'
    ser_txt = _seo_series_text(series_raw)  # SPV titles only
    tokens = [t.strip() for t in str(structure or '').split(',')]
    if 'Fund' in tokens:
        suffix = f'{ser_txt} SPV Units'.strip()
    elif 'Forward' in tokens:
        suffix = 'Forward Contract'
    elif 'Direct' in tokens or 'Direct Only' in tokens:
        suffix = 'Direct Transfer'
    else:
        suffix = ''
    title = f'{comp} Pre-IPO Stock' + (f' – {suffix}' if suffix else '')
    return _re.sub(r'\s+', ' ', title).strip()

def _seo_min_text(min_size):
    """' Min. $100K.' / ' Min. $2.5M.', or '' when missing, non-numeric or <= 0."""
    try:
        n = float(str(min_size).replace(',', '').replace('$', '').strip())
    except (TypeError, ValueError):
        return ''
    if not n > 0:
        return ''
    return f' Min. {_seo_money(n)}.'

def _seo_money(n):
    """100000 -> '$100K', 2500000 -> '$2.5M'."""
    if n >= 1_000_000:
        return '$' + f'{n / 1_000_000:.1f}'.rstrip('0').rstrip('.') + 'M'
    return '$' + f'{n / 1_000:.1f}'.rstrip('0').rstrip('.') + 'K'

def build_seo_description(deal_name, company_name, deal_type, structure, series_raw, min_size, is_closed):
    """Plain-text meta description, or None for skipped companies."""
    import re as _re
    comp = _seo_company(deal_name, company_name)
    if comp is None:
        return None
    ser_txt = _seo_series_text(series_raw)
    min_txt = _seo_min_text(min_size)
    tokens = [t.strip() for t in str(structure or '').split(',')]
    cta = ' Ask the seller questions or place a bid.'
    if is_closed:
        desc = f'This {comp} allocation has closed. See current pre-IPO offers in similar companies.'
    elif deal_type == 'Buy Order':
        desc = f'Active buyer for {comp} pre-IPO shares. Shareholders can submit an offer.'
    elif 'Fund' in tokens:
        desc = f'{comp} {ser_txt} SPV units for accredited investors.{min_txt}{cta}'
    elif 'Direct' in tokens or 'Direct Only' in tokens:
        desc = f'{comp} pre-IPO shares by direct transfer for accredited investors.{min_txt}{cta}'
    else:
        desc = f'{comp} pre-IPO stock for accredited investors.{min_txt}{cta}'
    return _re.sub(r' {2,}', ' ', desc).strip()

# --- Deal stage ------------------------------------------------------------
# PipelineCRM carries the stage on the deal itself. It normally arrives as a
# nested object (deal_stage: {"id": ..., "name": "Firm"}); some responses send
# just the name as a string. get_deal_stage_name accepts either and returns ''
# when the stage is absent, in which case no status is rendered at all.
STAGE_TOOLTIPS = {
    'firm':     'Price and size confirmed.',
    'inquiry':  'Awaiting key details.',
    'obsolete': 'Sold out or taken down. Bid to re-open.',
}

# Stages that turn the page into a closed-deal landing page for non-admins.
# Hold is NOT closed.
CLOSED_STAGES = {"obsolete", "won", "lost"}


def get_deal_stage_name(deal_data):
    for key in ('deal_stage', 'stage'):
        value = deal_data.get(key)
        if isinstance(value, dict):
            name = value.get('name') or ''
        elif isinstance(value, str):
            name = value
        else:
            continue
        if name.strip():
            return name.strip()
    return ''


# --- Other live offers (non-live deal pages) --------------------------------
LIVE_STAGES = {"firm", "matched", "inquiry"}
_LIVE_STAGE_RANK = {"firm": 0, "matched": 1, "inquiry": 2}


def _alt_num(v):
    """Tolerant number parse ('$1,000,000' -> 1000000.0); None when missing/unparseable/<= 0."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(str(v).replace('$', '').replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _alt_range(mn, mx):
    lo, hi = _alt_num(mn), _alt_num(mx)
    if lo is None and hi is None:
        return None
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    return (min(lo, hi), max(lo, hi))


def _alt_size_distance(a, b):
    if a is None or b is None:
        return None
    if a[1] < b[0]:
        return (b[0] - a[1]) / max(a[1], b[0])
    if b[1] < a[0]:
        return (a[0] - b[1]) / max(a[0], b[1])
    return 0.0


def _alt_struct_kind(structure):
    s = str(structure or '')
    if 'Fund' in s:
        return 'spv'
    if 'Direct' in s:
        return 'direct'
    return ''


def live_alternatives(deals, deal_id, company_name, deal_type, cur_structure, cur_min, cur_max, limit=5):
    """Live deals (Firm/Matched/Inquiry) on the same side in the same company,
    best match first. Pure: no I/O."""
    want_co = ' '.join(str(company_name or '').split()).lower()
    if not want_co:
        return []
    cur_kind = _alt_struct_kind(cur_structure)
    cur_rng = _alt_range(cur_min, cur_max)
    cands = []
    for d in deals or []:
        if not isinstance(d, dict):
            continue
        if str(d.get('id')) == str(deal_id):
            continue
        if ' '.join(str(d.get('company') or '').split()).lower() != want_co:
            continue
        if (d.get('type') or '').strip() != deal_type:
            continue
        if (d.get('stage') or '').strip().lower() not in LIVE_STAGES:
            continue
        cands.append(d)

    def _id_key(d):
        i = str(d.get('id') or '')
        return (0, int(i), '') if i.isdigit() else (1, 0, i)

    # Stable sorts, least significant key first.
    cands.sort(key=_id_key)
    cands.sort(key=lambda d: _LIVE_STAGE_RANK.get((d.get('stage') or '').strip().lower(), 9))
    cands.sort(key=lambda d: str(d.get('updated') or ''), reverse=True)

    def _primary(d):
        kind = _alt_struct_kind(d.get('structure'))
        match = 0 if (cur_kind and kind == cur_kind) else 1
        dist = _alt_size_distance(cur_rng, _alt_range(d.get('min_deal_size'), d.get('max_deal_size')))
        return (match, 1 if dist is None else 0, dist or 0.0)
    cands.sort(key=_primary)
    return cands[:limit]


def alt_live_link_text(d):
    side = 'Buy' if (d.get('type') or '').strip() == 'Buy Order' else 'Sell'
    parts = [side]
    lo, hi = _alt_num(d.get('min_deal_size')), _alt_num(d.get('max_deal_size'))
    lo_txt = _seo_money(lo) if lo is not None else None
    hi_txt = _seo_money(hi) if hi is not None else None
    if lo_txt and hi_txt and lo_txt != hi_txt:
        parts.append(f'{lo_txt}–{hi_txt}')
    elif lo_txt and hi_txt:
        parts.append(lo_txt)
    elif lo_txt:
        parts.append(f'from {lo_txt}')
    elif hi_txt:
        parts.append(f'up to {hi_txt}')
    kind = _alt_struct_kind(d.get('structure'))
    if kind == 'spv':
        parts.append('SPV')
    elif kind == 'direct':
        parts.append('Direct Transfer')
    return ' · '.join(parts)


def alt_live_gated_text(d):
    """Deal-gate link text: side and deal id only (no price, size or structure)."""
    side = 'Buy' if (d.get('type') or '').strip() == 'Buy Order' else 'Sell'
    return f"{side} · Deal {d.get('id')}"


ALT_BOX_STYLE = ('margin:24px 0;padding:20px 22px;border:1px solid var(--border-strong,#d8d4ca);'
                 'border-left:4px solid var(--accent,#3a5a75);border-radius:6px;background:#faf8f3')


def render_alt_live_box(stage_lower, is_closed_public, alts, company_display, deal_type, gated=False):
    """Box under the header for non-live deals. '' when nothing to show. With
    the deal gate on (gated) the links carry the side and deal id only."""
    if not stage_lower or stage_lower in LIVE_STAGES:
        return ''
    co = html_mod.escape(str(company_display or ''), quote=True)
    buy = deal_type == 'Buy Order'
    items = ''.join(
        f'<li><a href="{SIMILAR_TRADES_BASE}deal/{urllib.parse.quote(str(d.get("id")))}">'
        f'{html_mod.escape(alt_live_gated_text(d) if gated else alt_live_link_text(d), quote=True)}</a></li>'
        for d in alts)
    list_html = f'<ul class="alt-live-list">{items}</ul>'
    h2 = '<h2 style="margin:0 0 8px;font-size:20px">'
    if stage_lower in CLOSED_STAGES and is_closed_public:
        head = f'{h2}This allocation is no longer available</h2>\n'
        if alts:
            lead = f'Active buyers for {co}:' if buy else f'Live offers in {co}:'
            body = f'  <p style="margin:0 0 8px;color:var(--text-secondary,#555)">{lead}</p>\n  {list_html}\n'
        else:
            body = """  <p style="margin:0 0 16px;color:var(--text-secondary,#555)">Search for similar opportunities, or view all live indications (sign-in required).</p>
  <div style="display:flex;flex-wrap:wrap;gap:12px">
    <a href="#" class="btn bid-btn" onclick="var b=document.getElementById('dealSwitcherBtn');if(b){b.click();}return false;">Search similar opportunities</a>
    <a href="https://trades.graciagroup.com/" class="btn">View live indications</a>
  </div>
"""
        return f'<div class="closed-banner" style="{ALT_BOX_STYLE}">\n  {head}{body}</div>\n'
    if not alts:
        return ''
    title = f'Other active buyers for {co}' if buy else f'Other live offers in {co}'
    return f'<div class="closed-banner" style="{ALT_BOX_STYLE}">\n  {h2}{title}</h2>\n  {list_html}\n</div>\n'


STAGE_STATUS_CLASSES = {
    'firm':     'status-firm',      # green
    'inquiry':  'status-inquiry',   # amber
    'obsolete': 'status-obsolete',  # red
}


def render_stage_status(stage_name):
    """'Status: Firm' for the header metadata line, sharing that line with the
    deal id and updated date. Only the three known stages are coloured; any
    other stage renders neutral rather than guessing at its meaning."""
    if not stage_name:
        return ''

    key = stage_name.lower()
    css_class = STAGE_STATUS_CLASSES.get(key, 'status-neutral')
    tooltip = STAGE_TOOLTIPS.get(key, '')
    title_attr = f' title="{tooltip}"' if tooltip else ''
    return (f'<span class="deal-status"{title_attr}>&bull; Status: '
            f'<span class="{css_class}">{_sim_esc(stage_name)}</span></span>')


def map_option_value(field, value):
    options = {
        'Layers': {
            '7000228': 'SPV on cap table',
            '7000229': '2-Layer SPV',
            '7000230': '3-Layer SPV'
        },
        'GP SEC Registration': {
            '7000240': 'Yes',
            '7000241': 'No',
            '7000242': "Don't know"
        },
        'SPV Jurisdiction': {
            '7000243': 'Delaware',
            '7000244': 'US - Ex-Delaware',
            '7000254': 'Offshore',
            '7000245': 'Europe',
            '7000255': 'Other'
        },
        'SPVs Managed': {
            '7000249': 'This would be the first.',
            '7000250': '2-3',
            '7000251': '4-5',
            '7000252': '6-10',
            '7000253': 'More than 10'
        },
        'Fund Exemption': {
            '7200027': '3(c)(1) - Accredited Investors &amp; QPs',
            '7200028': '3(c)(7) - Qualified Purchasers only ($5M+ investments)',
            '7201486': 'Other'
        },
        'Class': {
            '5077831': 'Common',
            '5077834': 'Preferred',
            '5077912': 'Mixed',
            '5077915': 'Any'
        },
        'Seller Type': {
            '7020357': 'GP — syndicating new allocation',
            '7000232': 'GP — facilitating LP sale',
            '7000231': 'GP — selling own units',
            '7205050': 'GP — selling entire SPV',
            '7000233': 'LP — GP consent obtained',
            '7000234': 'LP — GP consent needed',
            '7000235': 'LP — platform holder'
        },
        'Price Status': {
            '7000238': 'Price is firm and unrelated to potential tender or round.',
            '7000239': 'Price is tied to upcoming tender or round.'
        },
        'Structure': {
            '5077906': 'Fund',
            '5077903': 'Forward',
            '5077900': 'Direct Only',
            '6250090': 'Direct',
            '6250093': 'No Forwards',
            '6361933': 'Unknown',
            '5077909': 'None'
        },
        'Messaging': {
            '7187010': 'Allow',
            '7187011': 'Disallow'
        },
        'Status': {
            '1': 'Open',
            '2': 'Won',
            '3': 'Inquiry',
            '4': 'Lost',
            '5': 'Dead'
        },
        'Type': {
            '5077819': 'Buy Order',
            '5011675': 'Sell Order'
        }
    }
    if field == 'Type' and isinstance(value, list):
        return ', '.join([options.get(field, {}).get(str(v), v) for v in value])
    if isinstance(value, list):
        return ', '.join([options.get(field, {}).get(str(v), v) for v in value])
    return options.get(field, {}).get(str(value), value)

def fetch_person_iqf_yes(person_id):
    """True if the deal's primary contact has IQF Status = Yes (6496840)."""
    if not person_id:
        return False
    url = f"https://api.pipelinecrm.com/api/v3/people/{person_id}.json"
    headers = {
        "Authorization": f"Bearer {JWT_TOKEN}",
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            person = json.loads(response.read().decode())
    except Exception as e:
        logger.error(f"IQF lookup failed for person {person_id}: {e}")
        return False
    v = (person.get("custom_fields") or {}).get("custom_label_3763008")
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v) == "6496840"

# --- Header onboarding status item (Seller KYC / Buyer accreditation) ------
# Read from syndicate-dash's slim people index in S3 -- never a live Pipeline
# call, never the full people.json. Module-level cache keyed on the object's
# ETag, HEAD at most once per PEOPLE_SLIM_TTL_SECONDS per warm container. Any
# S3 error (incl. AccessDenied) or missing data hides the item silently.
PEOPLE_SLIM_BUCKET = "full-pipeline-cache"
PEOPLE_SLIM_KEY = "syndicate-dash/people-slim.json"
PEOPLE_SLIM_TTL_SECONDS = 15 * 60
KYC_FIELD = "custom_label_3796440"            # CEF/ID: Yes 6600515, N/A 6600516
KYC_OK_IDS = {"6600515", "6600516"}
ACCREDITATION_FIELD = "custom_label_3763008"  # IQF Status: Yes 6496840, Unnecessary 6596073
ACCREDITATION_OK_IDS = {"6496840", "6596073"}
KYC_TOOLTIP = "The seller has completed Rainmaker Securities' client onboarding (KYC)."
ACCREDITATION_TOOLTIP = ("The buyer has submitted Rainmaker Securities' investor qualification form. "
                         "The information is self-reported and not independently verified.")
INVESTOR_LEVEL_FIELD = "custom_label_3923758"  # Investor Level (deal gate)
_people_slim_cache = {"etag": None, "checked_at": 0.0, "by_id": None, "by_email": None,
                      "dg_by_id": None, "last_modified": None}


def _person_emails(p):
    """Lowercased emails on a person record: every top-level email* field
    (string, list of strings, or list of {"email": ...} dicts)."""
    out = set()
    for k, v in p.items():
        if not isinstance(k, str) or not k.lower().startswith("email"):
            continue
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, dict):
                item = item.get("email") or item.get("address") or item.get("value")
            if isinstance(item, str) and "@" in item:
                out.add(item.strip().lower())
    return out


def _cf_option_ids(value):
    """A custom-field value (single id, list, or {"id": ...} dicts) as a set of id strings."""
    if value is None:
        return set()
    out = set()
    for item in (value if isinstance(value, list) else [value]):
        if isinstance(item, dict):
            item = item.get("option_id") or item.get("id") or item.get("value")
        if item not in (None, ""):
            out.add(str(item).split(".")[0])
    return out


def _dg_person_slim(p, cf):
    """(investor level ids, first name, full name, company, emails) for the deal gate."""
    first = (p.get("first_name") or "").strip() if isinstance(p.get("first_name"), str) else ""
    name = p.get("full_name") or p.get("name") or ""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        last = p.get("last_name") if isinstance(p.get("last_name"), str) else ""
        name = " ".join(x for x in (first, (last or "").strip()) if x)
    if not first and name:
        first = name.split()[0]
    co = p.get("company") if isinstance(p.get("company"), dict) else {}
    company = co.get("name") or p.get("company_name") or ""
    company = company.strip() if isinstance(company, str) else ""
    return (frozenset(_cf_option_ids(cf.get(INVESTOR_LEVEL_FIELD))), first, name, company,
            tuple(sorted(_person_emails(p))))


def _people_slim_status_index():
    """{person id str: (kyc_ok, accreditation_ok)} from people-slim.json, or
    None on any S3/parse failure. The same pass also fills
    _people_slim_cache["dg_by_id"] (Investor Level, names, company, emails) for
    the deal gate. Never reads people.json."""
    now = time.time()
    cache = _people_slim_cache
    if cache["by_id"] is not None and now - cache["checked_at"] < PEOPLE_SLIM_TTL_SECONDS:
        return cache["by_id"]
    try:
        s3 = boto3.client('s3')
        head = s3.head_object(Bucket=PEOPLE_SLIM_BUCKET, Key=PEOPLE_SLIM_KEY)
        etag = head.get("ETag")
        if cache["by_id"] is not None and etag and etag == cache["etag"]:
            cache["checked_at"] = now
            return cache["by_id"]
        obj = s3.get_object(Bucket=PEOPLE_SLIM_BUCKET, Key=PEOPLE_SLIM_KEY)
        data = json.loads(obj["Body"].read())
        by_id = {}
        by_email = {}
        dg_by_id = {}
        for p in (data.get("people") or []) if isinstance(data, dict) else []:
            if not isinstance(p, dict) or p.get("id") is None:
                continue
            cf = p.get("custom_fields") or {}
            by_id[str(p["id"])] = (bool(_cf_option_ids(cf.get(KYC_FIELD)) & KYC_OK_IDS),
                                   bool(_cf_option_ids(cf.get(ACCREDITATION_FIELD)) & ACCREDITATION_OK_IDS))
            dg_by_id[str(p["id"])] = _dg_person_slim(p, cf)
            for em in _person_emails(p):
                by_email.setdefault(em, set()).add(str(p["id"]))
        cache.update({"etag": etag or obj.get("ETag"), "checked_at": now, "by_id": by_id,
                      "by_email": by_email, "dg_by_id": dg_by_id,
                      "last_modified": head.get("LastModified") or obj.get("LastModified")})
        return by_id
    except Exception as e:
        logger.warning(f"people-slim.json read failed (status item hidden): {type(e).__name__}: {e}")
        return None


# --- Deal gate (shared verbatim with chadgracia/trades and chadgracia/CRMDealDetails) ---
# Offer terms are shown only to recognised investors. The switch lives in
# s3://full-pipeline-cache/deal-gate.json; with it off nothing changes for
# non-admins. gg_view (personal view-only links) only ever unlocks terms: it is
# never treated as signed in, never minted into gg_id and never yields an SSO token.
DG_BUCKET = "full-pipeline-cache"
DG_SWITCH_KEY = "deal-gate.json"
DG_SWITCH_TTL = 60
DG_REQUESTS_PREFIX = "deal-gate/requests/"
DG_SITE = "https://trades.graciagroup.com"
DG_VIEW_COOKIE = "gg_view"
DG_LINK_TTL = 183 * 86400
DG_COOKIE_TTL = 365 * 86400
DG_COOKIE_MAX_AGE = 31536000
DG_STATES = ("anon", "pending", "blocked", "cleared")
DG_IL_LABELS = {"6950561": "Unknown", "7161646": "Hold: Screen for Substantive",
                "6950562": "Non-Accredited", "7162165": "Substantive",
                "6950563": "Accredited Investor", "7209227": "Qualified Client",
                "6950564": "Qualified Purchaser"}
DG_IL_NON_ACCREDITED = "6950562"
DG_IL_HOLD = "7161646"
DG_PH_PRICE = "$00.00"
DG_PH_SIZE = "$000K"
DG_PH_PCT = "0.0%"
DG_PH_TEXT = "Xxxxxx"
DG_PH_NUM = "0,000"
DG_COGNITO_HOST = "https://us-east-1dsttcaqx7.auth.us-east-1.amazoncognito.com"
DG_COGNITO_CLIENT = "71vrglkidm13jb73u7nje3d1t2"
# Query parameters that never travel into a link, a redirect target or a log line.
DG_STRIP_PARAMS = ("v", "gp", "sso", "admin_key", "key", "code", "gate_preview", "state", "token")
_DG_TOKEN_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})\.(\d{1,12})\.([0-9a-f]{32})$")
_dg_switch_cache = {"enabled": None, "checked_at": 0.0}


def _dg_secret():
    """VIEW_LINK_SECRET from the Lambda environment ('' = personal links off)."""
    return os.environ.get("VIEW_LINK_SECRET", "") or ""


def dg_switch_enabled(s3=None, now=None):
    """Deal-gate switch. Cached DG_SWITCH_TTL seconds. A missing or unreadable
    object keeps the last value this container saw, else OFF. Never raises."""
    now = time.time() if now is None else now
    c = _dg_switch_cache
    if c["checked_at"] and now - c["checked_at"] < DG_SWITCH_TTL:
        return bool(c["enabled"])
    c["checked_at"] = now
    try:
        s3 = s3 or boto3.client('s3')
        obj = s3.get_object(Bucket=DG_BUCKET, Key=DG_SWITCH_KEY)
        data = json.loads(obj["Body"].read())
        val = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(val, bool):
            raise ValueError("no boolean 'enabled'")
        c["enabled"] = val
    except Exception as e:
        logger.info(f"deal-gate: switch unreadable ({type(e).__name__}); "
                    f"keeping {'ON' if c['enabled'] else 'OFF'}")
    return bool(c["enabled"])


def _dg_sig(prefix, pid, exp, secret):
    return hmac.new(secret.encode(), f"{prefix}|{pid}|{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def dg_make_token(pid, prefix="view1", ttl=DG_LINK_TTL, now=None):
    """'<pid>.<exp>.<sig>' or None when VIEW_LINK_SECRET is not set."""
    secret = _dg_secret()
    if not secret or not _DG_TOKEN_RE.match(f"{pid}.1.{'0' * 32}"):
        return None
    exp = int(time.time() if now is None else now) + int(ttl)
    return f"{pid}.{exp}.{_dg_sig(prefix, pid, exp, secret)}"


def dg_verify_token(token, prefix="view1", now=None):
    """pid for a well-formed, unexpired, correctly signed token, else None."""
    secret = _dg_secret()
    if not secret or not isinstance(token, str):
        return None
    m = _DG_TOKEN_RE.match(token)
    if not m:
        return None
    pid, exp, sig = m.group(1), m.group(2), m.group(3)
    if int(exp) <= int(time.time() if now is None else now):
        return None
    if not hmac.compare_digest(_dg_sig(prefix, pid, exp, secret), sig):
        return None
    return pid


def dg_view_cookie(pid, now=None):
    """Set-Cookie value for gg_view, or None when links are disabled."""
    tok = dg_make_token(pid, "viewc1", DG_COOKIE_TTL, now)
    if not tok:
        return None
    return (f"{DG_VIEW_COOKIE}={tok}; Max-Age={DG_COOKIE_MAX_AGE}; Path=/; "
            "Secure; HttpOnly; SameSite=Lax")


def dg_view_pid(event, now=None):
    """Person id from a valid gg_view cookie, else None. View-only."""
    return dg_verify_token(_get_cookie(event, DG_VIEW_COOKIE), "viewc1", now)


def _dg_query_pairs(event):
    raw = event.get("rawQueryString")
    if isinstance(raw, str):
        return urllib.parse.parse_qsl(raw, keep_blank_values=True)
    return list((event.get("queryStringParameters") or {}).items())


def dg_clean_path(path, fallback="/"):
    """A same-site relative path (with query) with link/secret parameters
    removed, else `fallback`."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return fallback
    if "\\" in path or any(ord(ch) < 32 for ch in path) or len(path) > 2000:
        return fallback
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc:
        return fallback
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k not in DG_STRIP_PARAMS]
    qs = urllib.parse.urlencode(pairs)
    return parts.path + (("?" + qs) if qs else "")


def dg_current_path(event, fallback="/"):
    """This request's path and query, without link/secret parameters."""
    path = (event.get("rawPath") or event.get("path")
            or (event.get("requestContext") or {}).get("http", {}).get("path") or "")
    if not path.startswith("/"):
        path = fallback.split("?", 1)[0]
    pairs = [(k, v) for k, v in _dg_query_pairs(event) if k not in DG_STRIP_PARAMS]
    qs = urllib.parse.urlencode(pairs)
    return path + (("?" + qs) if qs else "")


def dg_link_redirect(event, fallback="/", now=None):
    """For a valid, unexpired ?v= token: a 302 to the same URL without v (every
    other parameter kept) that sets gg_view. Else None (render normally)."""
    qp = event.get("queryStringParameters") or {}
    v = qp.get("v")
    if not v:
        return None
    pid = dg_verify_token(v, "view1", now)
    if not pid:
        logger.info("deal-gate: ignored invalid or expired personal link")
        return None
    cookie = dg_view_cookie(pid, now)
    if not cookie:
        return None
    path = (event.get("rawPath") or event.get("path")
            or (event.get("requestContext") or {}).get("http", {}).get("path") or "")
    if not path.startswith("/"):
        path = fallback.split("?", 1)[0]
    pairs = [(k, val) for k, val in _dg_query_pairs(event) if k != "v"]
    qs = urllib.parse.urlencode(pairs)
    resp = {"statusCode": 302,
            "headers": {"Location": path + (("?" + qs) if qs else ""),
                        "Cache-Control": "private, no-store"},
            "body": ""}
    logger.info("deal-gate: personal link accepted; gg_view set")
    return dg_add_cookies(resp, event, [cookie])


def dg_add_cookies(response, event, cookies):
    """Add Set-Cookie values to a response without dropping any already there.
    Payload v2 (Function URL / HTTP API) uses the `cookies` list; payload v1
    (REST API) needs multiValueHeaders, because a plain header holds only one."""
    cookies = [c for c in (cookies or []) if c]
    if not cookies:
        return response
    out = dict(response)
    if event.get("version") == "2.0" or "rawPath" in event or "cookies" in event:
        out["cookies"] = list(out.get("cookies") or []) + cookies
        return out
    headers = dict(out.get("headers") or {})
    mv = {k: list(v) for k, v in (out.get("multiValueHeaders") or {}).items()}
    existing = mv.pop("Set-Cookie", [])
    if "Set-Cookie" in headers:
        existing = [headers.pop("Set-Cookie")] + existing
    mv["Set-Cookie"] = existing + cookies
    out["headers"] = headers
    out["multiValueHeaders"] = mv
    return out


def dg_people():
    """(by_email, dg_by_id, last_modified) from people-slim.json, or None."""
    if _people_slim_status_index() is None:
        return None
    c = _people_slim_cache
    if c.get("dg_by_id") is None or c.get("by_email") is None:
        return None
    return c["by_email"], c["dg_by_id"], c.get("last_modified")


def dg_level_state(level_ids):
    ids = {str(i) for i in (level_ids or ())}
    if DG_IL_NON_ACCREDITED in ids:
        return "blocked"
    if DG_IL_HOLD in ids:
        return "pending"
    return "cleared"


def dg_level_label(level_ids):
    ids = sorted(str(i) for i in (level_ids or ()))
    if not ids:
        return "Blank"
    return " / ".join(DG_IL_LABELS.get(i, f"Other ({i})") for i in ids)


def dg_combine(states):
    """Several matched people: cleared if any, else blocked if any, else pending."""
    states = list(states)
    if "cleared" in states:
        return "cleared"
    if "blocked" in states:
        return "blocked"
    return "pending"


def dg_state_for_email(email, people):
    by_email, dg_by_id, _ = people
    pids = by_email.get((email or "").strip().lower()) or ()
    if not pids:
        return "pending", None
    states = {pid: dg_level_state((dg_by_id.get(pid) or ((),))[0]) for pid in pids}
    st = dg_combine(states.values())
    pick = sorted(p for p, s in states.items() if s == st)
    return st, (pick[0] if pick else None)


def dg_viewer_state(event, view_pid=None):
    """'anon' | 'pending' | 'blocked' | 'cleared' for a non-admin request.
    people-slim.json is read only when the request carries an identity."""
    email = _read_identity_email(event)
    view_pid = view_pid or dg_view_pid(event)
    if not email and not view_pid:
        return "anon"
    people = dg_people()
    if people is None:
        logger.warning("deal-gate: people index unreadable; identified viewer treated as cleared")
        return "cleared"
    states = []
    if email:
        states.append(dg_state_for_email(email, people)[0])
    if view_pid:
        rec = people[1].get(str(view_pid))
        states.append(dg_level_state(rec[0]) if rec else "pending")
    return dg_combine(states)


def dg_decide(event, qp, is_admin, view_pid=None):
    """(gated, state, preview). Admin preview (&gate_preview=...) works whatever
    the switch; otherwise the gate applies only when the switch is ON and the
    viewer is not cleared. With the switch OFF a non-admin request does no
    people lookup at all."""
    prev = (qp or {}).get("gate_preview")
    if is_admin and prev in DG_STATES:
        return prev != "cleared", prev, True
    if is_admin:
        return False, "cleared", False
    if not dg_switch_enabled():
        return False, None, False
    state = dg_viewer_state(event, view_pid)
    return state != "cleared", state, False


def dg_ph(text):
    """A fixed placeholder, blurred by CSS. Never the real value."""
    return f'<span class="gated">{text}</span>'


def dg_signup_url(dest_url):
    """Cognito sign-up URL that round-trips to dest_url via `state` (same as the Bid modal)."""
    state = base64.urlsafe_b64encode(dest_url.encode()).decode().rstrip("=")
    return (f"{DG_COGNITO_HOST}/signup?client_id={DG_COGNITO_CLIENT}&response_type=code"
            "&scope=openid+email&redirect_uri=https://trades.graciagroup.com"
            f"&state={urllib.parse.quote(state, safe='')}")


DG_CSS = """<style>
.gated{filter:blur(5px);-webkit-filter:blur(5px);user-select:none;color:var(--text-secondary,#666)}
.gate-box{margin:16px 0;padding:18px 22px;border:1px solid var(--border-strong,#d8d4ca);
  border-left:4px solid var(--accent,#3a5a75);border-radius:6px;background:#faf8f3;font-size:14px}
.gate-box h2{margin:0 0 8px;font-size:18px}
.gate-box p{margin:6px 0}
.gate-form{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.gate-form input[type=email]{flex:1 1 220px;padding:8px 10px;font-size:14px;border:1px solid #ccc;border-radius:5px}
.gate-form button{padding:8px 16px;font-size:14px;font-weight:600;border:none;border-radius:5px;
  background:var(--accent,#3a5a75);color:#fff;cursor:pointer}
.gate-hp{position:absolute!important;left:-9999px!important;width:1px;height:1px;overflow:hidden}
</style>"""

DG_JS = """<script>
function ggGateSubmit(f){
  try{
    var b=new URLSearchParams();
    b.append('email',f.elements['email'].value||'');
    b.append('website',f.elements['website'].value||'');
    b.append('path',window.location.pathname+window.location.search);
    fetch('/?view=view-link-request',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b.toString()}).catch(function(){});
  }catch(e){}
  f.style.display='none';
  var d=f.parentNode.querySelector('.gate-done'); if(d){d.hidden=false;}
  return false;
}
</script>"""


def dg_gate_box(state, dest_url):
    """The gate box. Copy is fixed; never mentions tracking, the CRM or levels."""
    if state in ("pending", "blocked"):
        return (DG_CSS + '<div class="gate-box"><p>Thanks for registering. Your access is being '
                "reviewed and we'll be in touch.</p></div>")
    signup = html_mod.escape(dg_signup_url(dest_url), quote=True)
    return (DG_CSS + DG_JS
            + '<div class="gate-box">'
            '<h2>Offer terms are shown to investors we work with</h2>'
            "<p>Already working with us? Enter your email and we'll send you a one-click link.</p>"
            '<form class="gate-form" method="post" onsubmit="return ggGateSubmit(this)">'
            '<input type="email" name="email" required placeholder="Email" aria-label="Email">'
            '<input type="text" name="website" class="gate-hp" tabindex="-1" autocomplete="off" aria-hidden="true">'
            '<button type="submit">Send link</button>'
            '</form>'
            '<p class="gate-done" hidden>If that address is on file, a link is on its way.</p>'
            f'<p>New here? <a href="{signup}">Request access</a></p>'
            '</div>')


DG_ADMIN_BOX = """<div class="gg-copylink" style="margin:12px 0;padding:10px 14px;border:1px dashed #b0a98f;border-radius:6px;background:#fffdf5;font-size:13px;max-width:560px">
<strong>Copy link for&hellip;</strong>
<input type="text" id="ggCopyQ" placeholder="Name, company or email" style="width:100%;box-sizing:border-box;margin-top:6px;padding:6px 8px;font-size:13px">
<div id="ggCopyRes" style="margin-top:6px"></div>
</div>
<script>
(function(){
  var q=document.getElementById('ggCopyQ'), res=document.getElementById('ggCopyRes'), t=null;
  if(!q||!res) return;
  var STRIP=['v','gp','sso','admin_key','key','code','gate_preview','state','token'];
  function dest(){
    var u=new URL(window.location.href);
    STRIP.forEach(function(k){u.searchParams.delete(k);});
    return u.pathname+u.search;
  }
  function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function copy(text){
    if(navigator.clipboard&&window.isSecureContext){return navigator.clipboard.writeText(text);}
    return new Promise(function(ok,no){var ta=document.createElement('textarea');ta.value=text;
      ta.style.position='fixed';ta.style.top='-1000px';document.body.appendChild(ta);ta.select();
      var r=false;try{r=document.execCommand('copy');}catch(e){}document.body.removeChild(ta);r?ok():no();});
  }
  function pick(pid,row){
    fetch('/?view=view-link&pid='+encodeURIComponent(pid)+'&dest='+encodeURIComponent(dest()),{credentials:'same-origin'})
      .then(function(r){return r.json();})
      .then(function(j){
        if(!j||!j.link){row.querySelector('.ggc-msg').textContent=(j&&j.error)||'No link';return;}
        copy(j.link).then(function(){row.querySelector('.ggc-msg').textContent='Copied';},
                          function(){row.querySelector('.ggc-msg').textContent='Copy failed';});
      }).catch(function(){row.querySelector('.ggc-msg').textContent='Failed';});
  }
  function search(){
    var s=q.value.trim(); if(s.length<2){res.innerHTML='';return;}
    fetch('/?view=view-link&q='+encodeURIComponent(s),{credentials:'same-origin'})
      .then(function(r){return r.json();})
      .then(function(j){
        if(!j||!j.matches){res.textContent=(j&&j.error)||'Not available';return;}
        if(!j.matches.length){res.textContent='No matches';return;}
        res.innerHTML=j.matches.map(function(m){
          var warn=m.state==='cleared'?'':' <span style="color:#b45309">('+esc(m.state)+': this link will not unlock terms)</span>';
          return '<div class="ggc-row" data-pid="'+esc(m.pid)+'" style="padding:4px 0;cursor:pointer;border-bottom:1px solid #eee">'+
            esc(m.name)+' &middot; '+esc(m.company)+' &middot; '+esc(m.email)+' &middot; '+esc(m.state)+warn+
            ' <span class="ggc-msg" style="color:#16a34a"></span></div>';
        }).join('');
        Array.prototype.forEach.call(res.querySelectorAll('.ggc-row'),function(row){
          row.addEventListener('click',function(){pick(row.getAttribute('data-pid'),row);});
        });
      }).catch(function(){res.textContent='Search failed';});
  }
  q.addEventListener('input',function(){clearTimeout(t);t=setTimeout(search,250);});
})();
</script>"""


def dg_redacted_event(event):
    """The event for logging: link/secret query values, cookies and the body removed."""
    try:
        ev = dict(event or {})
        qp = ev.get("queryStringParameters")
        if isinstance(qp, dict):
            ev["queryStringParameters"] = {k: ("[redacted]" if k in DG_STRIP_PARAMS else v)
                                           for k, v in qp.items()}
        if isinstance(ev.get("rawQueryString"), str):
            pairs = urllib.parse.parse_qsl(ev["rawQueryString"], keep_blank_values=True)
            ev["rawQueryString"] = urllib.parse.urlencode(
                [(k, "[redacted]" if k in DG_STRIP_PARAMS else v) for k, v in pairs])
        if isinstance(ev.get("cookies"), list):
            ev["cookies"] = [str(c).split("=", 1)[0] + "=[redacted]" for c in ev["cookies"]]
        for hk in ("headers", "multiValueHeaders"):
            if isinstance(ev.get(hk), dict):
                ev[hk] = {k: ("[redacted]" if str(k).lower() in ("cookie", "authorization") else v)
                          for k, v in ev[hk].items()}
        if "body" in ev:
            ev["body"] = "[omitted]"
        for k in ("multiValueQueryStringParameters",):
            if isinstance(ev.get(k), dict):
                ev[k] = {kk: (["[redacted]"] if kk in DG_STRIP_PARAMS else vv) for kk, vv in ev[k].items()}
        return ev
    except Exception:
        return {"note": "event not loggable"}
# --- end deal gate shared block ---


def _deal_linked_person_ids(deal_data):
    """Every person linked to the deal record already fetched: primary contact
    plus the linked people list (whichever shape the record carries)."""
    ids = []
    def add(v):
        if v not in (None, "") and str(v) not in ids:
            ids.append(str(v))
    add((deal_data.get('primary_contact') or {}).get('id') if isinstance(deal_data.get('primary_contact'), dict) else None)
    add(deal_data.get('primary_contact_id'))
    for p in deal_data.get('people') or []:
        add(p.get('id') if isinstance(p, dict) else p)
    for pid in deal_data.get('person_ids') or []:
        add(pid)
    return ids


def render_onboarding_status(deal_type, person_ids, index):
    """' • Seller KYC: Complete|Pending' (Sell) or ' • Buyer accreditation:
    On file|Pending' (Buy), or '' when there's no type, no linked people or no
    index. Grey like the Deal ID line; only the positive value is green."""
    if deal_type not in ("Sell Order", "Buy Order") or not person_ids or index is None:
        return ''
    flags = [index.get(pid) for pid in person_ids]
    if deal_type == "Sell Order":
        ok = any(f and f[0] for f in flags)
        label, good, tip = "Seller KYC", "Complete", KYC_TOOLTIP
    else:
        ok = any(f and f[1] for f in flags)
        label, good, tip = "Investor Qualification Form", "Submitted", ACCREDITATION_TOOLTIP
    value = (f'<span class="onboard-ok">{good}</span>' if ok else 'Pending')
    return f'<span class="onboard-status" title="{html_mod.escape(tip, quote=True)}"> &bull; {label}: {value}</span>'


# --- Unconfirmed rule (shared across repos) ----------------------------------
LAST_CONFIRMED_FIELD = "custom_label_4012491"   # "YYYY/MM/DD"
REFRESH_FIELD        = "custom_label_3994687"   # days; 0 = never Unconfirmed
SELLER_ROLE_FIELD    = "custom_label_3938748"
SELLER_ROLE_GP_ALLOC = "7020357"                # GP — syndicating new allocation
MESSAGING_FIELD      = "custom_label_4001285"
MESSAGING_ALLOW      = "7187010"
HOLD_STAGE_ID        = "2094373"
UNCONFIRMED_CLOSED_STAGES = CLOSED_STAGES | {"trade broken", "archived"}


def _deal_stage_id(deal_data):
    v = deal_data.get('deal_stage')
    if isinstance(v, dict) and v.get('id') is not None:
        return str(v.get('id'))
    if deal_data.get('deal_stage_id') is not None:
        return str(deal_data.get('deal_stage_id'))
    return ''


RESTORE_LOG_BUCKET     = "full-pipeline-cache"
RESTORE_LOG_KEY        = "obsolete-review/log.json"   # written by stale-deal-scanner
RESTORE_LOG_TTL        = 600                          # seconds
RESTORE_GRACE_MINUTES  = 15
_restore_log_cache = {"by_deal": None, "loaded_at": 0.0}


def _parse_iso_utc(value):
    """ISO timestamp -> aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _restore_log_latest():
    """{str(deal_id): latest log entry} from the obsolete-review log, cached 10 min.
    Never raises: missing/unreadable/malformed log -> {} (no restores)."""
    cache = _restore_log_cache
    now = time.time()
    if cache["by_deal"] is not None and now - cache["loaded_at"] < RESTORE_LOG_TTL:
        return cache["by_deal"]
    by_deal, best = {}, {}
    try:
        obj = boto3.client('s3').get_object(Bucket=RESTORE_LOG_BUCKET, Key=RESTORE_LOG_KEY)
        entries = json.loads(obj["Body"].read()).get("entries") or []
        floor = datetime.min.replace(tzinfo=timezone.utc)
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or e.get("deal_id") is None:
                continue
            did = str(e.get("deal_id")).strip()
            key = (_parse_iso_utc(e.get("at")) or floor, i)
            if did not in best or key >= best[did]:
                best[did], by_deal[did] = key, e
    except Exception as e:
        logger.info(f"restore log: unavailable ({e!r})")
        by_deal = {}
    cache["by_deal"], cache["loaded_at"] = by_deal, now
    return by_deal


def unconfirmed_status(deal_data, today=None):
    """(is_unconfirmed, effective_date_or_None). Effective date = newer of Last
    Confirmed and the UTC date of updated_at (restore exception applies).
    Fail-soft: any unparseable input the rule needs returns (False, None)."""
    try:
        stage = get_deal_stage_name(deal_data).strip().lower()
        if (_deal_stage_id(deal_data) == HOLD_STAGE_ID or stage == "hold"
                or stage in UNCONFIRMED_CLOSED_STAGES
                or deal_data.get('is_archived') or deal_data.get('archived')):
            return False, None
        cf = deal_data.get('custom_fields') or {}
        if not isinstance(cf, dict):
            return False, None
        refresh = cf.get(REFRESH_FIELD)
        if isinstance(refresh, list):
            refresh = refresh[0] if refresh else None
        if refresh not in (None, ""):
            threshold = int(float(str(refresh).strip()))
            if threshold <= 0:
                return False, None
        else:
            threshold = 30 if SELLER_ROLE_GP_ALLOC in _cf_option_ids(cf.get(SELLER_ROLE_FIELD)) else 60
        raw = cf.get(LAST_CONFIRMED_FIELD)
        confirmed = None
        if raw is not None and str(raw).strip() != "":
            txt = str(raw).strip()[:10].replace('-', '/')
            confirmed = datetime.strptime(txt, "%Y/%m/%d").date()
        # Any Pipeline edit counts as confirmation, except the restore's own write:
        # if the deal's latest obsolete-review log entry is "restored", updated_at
        # counts only when it is more than 15 minutes after that restore.
        updated = None
        upd_raw = deal_data.get('updated_at')
        if upd_raw not in (None, ""):
            upd_dt = datetime.strptime(str(upd_raw).strip(), '%Y/%m/%d %H:%M:%S %z').astimezone(timezone.utc)
            entry = _restore_log_latest().get(str(deal_data.get('id')))
            if entry is None or entry.get('action') != 'restored':
                updated = upd_dt.date()
            else:
                at = _parse_iso_utc(entry.get('at'))
                if at is not None and upd_dt > at + timedelta(minutes=RESTORE_GRACE_MINUTES):
                    updated = upd_dt.date()
        dates = [d for d in (confirmed, updated) if d is not None]
        if not dates:
            return True, None
        effective = max(dates)
        today = today or datetime.utcnow().date()
        return (today - effective).days > threshold, effective
    except Exception as e:
        logger.info(f"unconfirmed_status: skipped ({e!r})")
        return False, None


def render_unconfirmed_notice(confirmed, deal_type, show_ask):
    """Slim amber one-line notice under the deal header ('' never errors the page)."""
    try:
        if confirmed:
            msg = (f"Unconfirmed — last confirmed {confirmed.strftime('%b')} {confirmed.day}, "
                   f"{confirmed.year}. This order may no longer be available.")
        else:
            msg = "Unconfirmed — not recently confirmed. This order may no longer be available."
        btn = ''
        if show_ask:
            who = "buyer" if deal_type == "Buy Order" else "seller"
            btn = (
                '<button type="button" class="unconfirmed-ask" onclick="'
                "var b=document.querySelector('.qa-box');"
                "var c=document.querySelector('.qa-box input[name=q_still_active]');"
                "if(c){c.checked=true;}"
                "if(b){b.scrollIntoView({behavior:'smooth',block:'start'});}"
                f'">Ask the {who} to confirm</button>'
            )
        return (
            '<div class="unconfirmed-notice">'
            f'<span class="unconfirmed-clock" title="&#9201; {html_mod.escape(msg)}">&#9201;</span>'
            f'<span class="unconfirmed-text">{html_mod.escape(msg)}</span>'
            f'{btn}</div>'
        )
    except Exception as e:
        logger.info(f"render_unconfirmed_notice: skipped ({e!r})")
        return ''


def render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room=True, owner_iqf_yes=False, est_val=None, unconfirmed=False):
    """Build the right-hand 'Questions about this deal' box (display only).

    Picks the buyer or seller question set based on the deal type, marks
    questions that can be auto-answered from existing deal fields, and renders
    an 'Ask' mailto button (built like the existing Bid button) for the rest.
    """
    if deal_type == "Sell Order":
        catalog = QUESTION_CATALOG_BUYER
    elif deal_type == "Buy Order":
        catalog = QUESTION_CATALOG_SELLER
    else:
        catalog = QUESTION_CATALOG_BUYER

    structure = map_option_value('Structure', mapped_fields.get('Structure', []))
    if isinstance(structure, (list, tuple)):
        structure = ', '.join(str(x) for x in structure)
    elif not isinstance(structure, str):
        structure = str(structure or '')
    is_spv = 'Fund' in structure

    # Match the Forward option by id, not by label: the 'No Forwards' label
    # contains 'Forward' as a substring.
    structure_ids = mapped_fields.get('Structure', [])
    if not isinstance(structure_ids, (list, tuple)):
        structure_ids = [structure_ids]
    is_forward = '5077903' in [str(x) for x in structure_ids]

    layers = map_option_value('Layers', mapped_fields.get('Layers', []))
    if isinstance(layers, (list, tuple)):
        layers = ', '.join(str(x) for x in layers)
    elif not isinstance(layers, str):
        layers = str(layers or '')
    is_multilayer = ('2-Layer' in layers) or ('3-Layer' in layers)
    is_tender = str(mapped_fields.get('Price Status', '')) == '7000239'

    catalog = [it for it in catalog
               if not (is_spv and it["id"] == "direct_trade")
               and not (it["id"] == "nda_l1" and not is_multilayer)
               and not (is_spv and it["id"] == "accept_fund")]

    FORM_URL = DESK_URL + "/update/"

    rows = ""
    for item in catalog:
        qid = item["id"]
        question_text = item["q"]

        if qid == "still_active" and not unconfirmed:
            continue
        # If the answer is already visible in the left-hand tables, omit the question.
        if qid == "class" and mapped_fields.get('Class'):
            continue
        if qid == "min_max" and mapped_fields.get('Min Deal Size') and mapped_fields.get('Max Deal Size'):
            continue
        # SPVs sell units, not shares; min/max size is asked as min_ticket / max_ticket.
        if qid in ("shares_avail", "min_max") and is_spv:
            continue
        if qid == "shares_avail" and mapped_fields.get('Shares'):
            continue
        if qid == "max_ticket" and (not is_spv or mapped_fields.get('Max Deal Size')):
            continue
        if qid == "min_ticket" and (not is_spv or mapped_fields.get('Min Deal Size')):
            continue
        # Blank is the only case asked: >0 is shown on the page, 0 = seller said unknown.
        if qid == "est_valuation" and (not is_spv or est_val is not None):
            continue
        if qid == "est_valuation" and str(mapped_fields.get('Seller Type', '')) == '7020357':
            question_text = "What is the round valuation (pre-money)?"
        if qid == "seller_fee" and (mapped_fields.get('Seller Fee') or not is_spv):
            continue
        if qid == "fee_structure" and not is_spv:
            continue
        if qid == "accept_bid" and is_tender:
            continue
        if qid == "accept_bid" and deal_type == "Sell Order" and str(mapped_fields.get('Seller Type', '')) == '7020357':
            continue
        # A forward exists precisely because the company won't permit a direct
        # transfer, so asking about direct-transfer permission is moot.
        if qid == "direct_trade" and is_forward:
            continue
        if qid == "move_bid_up":
            continue
        if qid == "qp_accredited" and not is_spv:
            continue
        if qid == "fund_exemption" and (not is_spv or mapped_fields.get('Fund Exemption')):
            continue
        if qid == "no_data_room" and not ask_data_room:
            continue
        if qid == "data_room_avail" and not ask_data_room:
            continue
        if qid == "iqf_done" and owner_iqf_yes:
            continue

        has_fields = qid in ("accept_bid", "fee_structure")
        row_cls = "qa-row no-line" if has_fields else "qa-row"
        rows += (
            f'<label class="{row_cls}">'
            f'<input type="checkbox" name="q_{qid}" value="1">'
            f'<span class="qa-q">{question_text}</span>'
            f'</label>'
        )
        if qid == "accept_bid":
            rows += (
                '<div class="qa-bid">'
                '<input type="number" name="bid_amount" step="any" placeholder="Bid $/share (gross)">'
                '<input type="number" name="bid_size" step="any" placeholder="Size $ (opt)">'
                '</div>'
                '<div class="qa-bid-note">'
                + ("Gross = what the buyer pays. Your proceeds are net of commission."
                   if deal_type == "Buy Order" else
                   "Gross = the all-in price per share you'd pay.")
                + '</div>'
            )
        if qid == "fee_structure":
            # Pre-fill with the deal's current fees; leave blank if missing (preserve 0).
            sf = mapped_fields.get('Seller Fee')        # One-time (custom_label_3940560)
            mf = mapped_fields.get('Management Fee')    # Man
            cr = mapped_fields.get('Carry')             # Carry
            sf = '' if sf is None else sf
            mf = '' if mf is None else mf
            cr = '' if cr is None else cr
            rows += (
                '<div class="qa-fees">'
                f'<label>One-time<input type="text" inputmode="decimal" name="fee_onetime" value="{sf}"></label>'
                f'<label>Man<input type="text" inputmode="decimal" name="fee_man" value="{mf}"></label>'
                f'<label>Carry<input type="text" inputmode="decimal" name="fee_carry" value="{cr}"></label>'
                '<label>Ticket $<input type="number" name="fee_ticket" step="any" placeholder="e.g. 1000000"></label>'
                '</div>'
            )

    if not rows:
        return ''

    return (
        '<aside class="qa-box">'
        f'<h2>Send Question to {"Buyer" if deal_type == "Buy Order" else "Seller"}</h2>'
        f'<form method="POST" action="{FORM_URL}">'
        '<input type="hidden" name="qa" value="submit">'
        f'<input type="hidden" name="deal_id" value="{deal_id}">'
        + "<label class=\"qa-row\" style=\"font-weight:600\"><input type=\"checkbox\" onclick='var b=this.checked;this.closest(\"form\").querySelectorAll(\"input[name^=q_]\").forEach(function(c){c.checked=b});'><span class=\"qa-q\">Select all</span></label>"
        + rows
        + '<input type="text" name="buyer_name" placeholder="Your name" class="qa-email">'
        + '<input type="email" name="buyer_email" placeholder="Your email (for the answers)" required class="qa-email">'
        + '<button type="submit" class="qa-send">Send</button>'
        + '</form>'
        + '</aside>'
    )

def render_weekly_signup(deal_id, deal_name):
    """Quiet email-capture card at the bottom of the side column. POSTs to the
    desk update Lambda (signup=weekly), which emails Chad — no CRM write."""
    safe_deal_name = html_mod.escape(str(deal_name or ''), quote=True)
    return (
        '<div class="weekly-box">'
        '<div class="weekly-text">'
        '<h2>Accredited investor?</h2>'
        '<p class="weekly-lead">Get the weekly list of every live trade &mdash; prices, structures, and new offerings, every Monday.</p>'
        '</div>'
        f'<form method="POST" action="{DESK_URL}/update/" class="weekly-form">'
        '<input type="hidden" name="signup" value="weekly">'
        f'<input type="hidden" name="deal_id" value="{deal_id}">'
        f'<input type="hidden" name="deal_name" value="{safe_deal_name}">'
        '<input type="text" name="website" class="weekly-hp" tabindex="-1" autocomplete="off" aria-hidden="true">'
        '<input type="email" name="signup_email" placeholder="Your email" required class="qa-email">'
        '<button type="submit" class="qa-send">Get the list</button>'
        '</form>'
        '<p class="weekly-note">For accredited investors only. Unsubscribe anytime.</p>'
        '</div>'
    )


def test_news_api(company_name, max_articles=5):
    """Fetch news articles for a given company using urllib."""
    # Construct the URL for the API call
    url = f"https://newsapi.org/v2/everything?q={urllib.parse.quote(company_name)}&from={(datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')}&sortBy=publishedAt&pageSize={max_articles}&language=en&apiKey={API_KEY}"
    req = urllib.request.Request(url)

    try:
        # Make the API call
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                # Parse and return JSON response if successful
                return json.loads(response.read().decode())
            else:
                # Log error if API call fails
                logger.error(f"API Error: {response.status} - {response.read().decode()}")
                return None
    except urllib.error.URLError as e:
        # Log URLError if the request fails
        logger.error(f"Request failed: {e}")
        return None

if __name__ == "__main__":
    news_data = test_news_api("SpaceX")
    print(news_data)

# --- Partner Desk pop-up ----------------------------------------------------
# Intermediaries (co-brokers / foreign finders) arriving with a signed handoff
# token (?gp= or the gg_pd cookie) get a blocking overlay after 5s. Everything
# here is additive: it runs only when gp / gg_pd / pd_preview is present, reads
# S3 only (never writes), and any failure returns the normal page unchanged.
PD_SECRET_BUCKET = "pipeline-token"
PD_SECRET_KEY = "partner-desk-secret.json"
PD_ACCESS_BUCKET = "full-pipeline-cache"
PD_ACCESS_KEY = "partner-desk/access.json"
PD_ACCESS_TTL_SECONDS = 15 * 60
PD_EXCLUDED_PD = {7218523, 7218528}   # Investor, Partner – Registered
PD_COOKIE = "gg_pd"
PD_COOKIE_MAX_AGE = 2592000
PD_BEACON_URL = "https://bddpwqsqvt32ritxpjqlqwhaim0ykbol.lambda-url.us-east-1.on.aws/?view=pd-event"
PD_FULL_BOOK_URL = "https://trades.graciagroup.com/?view=partner-desk"
PD_IQF_INDIVIDUAL_URL = "https://www.rainmakersecurities.com/investor-qualification-form-for-natural-persons"
PD_IQF_ENTITY_URL = "https://www.rainmakersecurities.com/investor-qualification-form-for-entity-persons"
PD_LEAVE_URL = "https://www.graciagroup.com"
_PD_TOKEN_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})\.(\d{1,12})\.([0-9a-f]{32})$")
PD_RETRY_SECONDS = 60
_pd_secret_cache = {"secret": None, "failed_at": 0.0, "error": None}
_pd_access_cache = {"people": None, "loaded_at": 0.0, "failed_at": 0.0, "error": None}


def _pd_load_secret():
    """(secret, None) or (None, reason). Read-only; cached once read. A failed
    read is retried at most once every PD_RETRY_SECONDS."""
    cache = _pd_secret_cache
    if cache["secret"]:
        return cache["secret"], None
    now = time.time()
    if cache["failed_at"] and now - cache["failed_at"] < PD_RETRY_SECONDS:
        return None, f"{cache['error']} (retry pending)"
    try:
        obj = boto3.client('s3').get_object(Bucket=PD_SECRET_BUCKET, Key=PD_SECRET_KEY)
        secret = json.loads(obj['Body'].read()).get("secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError("no 'secret' string in file")
    except Exception as e:
        cache.update({"failed_at": now, "error": f"{type(e).__name__}: {e}"})
        return None, cache["error"]
    cache.update({"secret": secret, "failed_at": 0.0, "error": None})
    return secret, None


def _pd_load_access():
    """(people dict, None) or (None, reason). A good load is cached
    PD_ACCESS_TTL_SECONDS; a failed load is retried after PD_RETRY_SECONDS,
    and any previous good copy keeps being used until a refresh succeeds."""
    cache = _pd_access_cache
    now = time.time()
    if cache["people"] is not None and now - cache["loaded_at"] < PD_ACCESS_TTL_SECONDS:
        return cache["people"], None
    if cache["failed_at"] and now - cache["failed_at"] < PD_RETRY_SECONDS:
        if cache["people"] is not None:
            return cache["people"], None
        return None, f"{cache['error']} (retry pending)"
    try:
        obj = boto3.client('s3').get_object(Bucket=PD_ACCESS_BUCKET, Key=PD_ACCESS_KEY)
        data = json.loads(obj['Body'].read())
        people = data.get("people") if isinstance(data, dict) else None
        if not isinstance(people, dict):
            raise ValueError("no 'people' object in file")
    except Exception as e:
        cache.update({"failed_at": now, "error": f"{type(e).__name__}: {e}"})
        if cache["people"] is not None:
            logger.warning(f"pd: access.json refresh failed, using previous copy: {cache['error']}")
            return cache["people"], None
        return None, cache["error"]
    cache.update({"people": people, "loaded_at": now, "failed_at": 0.0, "error": None})
    return people, None


def _pd_verify_token(token, secret):
    """pid when token is 'pid.exp.sig', unexpired and correctly signed; else (None, reason)."""
    m = _PD_TOKEN_RE.match(token or "")
    if not m:
        return None, "malformed token"
    pid, exp, sig = m.group(1), m.group(2), m.group(3)
    if int(exp) <= int(time.time()):
        return None, "expired token"
    expected = hmac.new(secret.encode(), f"{pid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None, "bad signature"
    return pid, None


def _pd_is_intermediary(rec):
    if not isinstance(rec, dict) or rec.get("intermediary") is not True:
        return False
    pd_val = rec.get("pd")
    try:
        pd_num = int(pd_val) if pd_val is not None else None
    except (TypeError, ValueError):
        pd_num = None
    return pd_num not in PD_EXCLUDED_PD


def _pd_gg_id_matches(email, pid, rec):
    """True only when the signed-in gg_id email provably belongs to pid."""
    em = email.strip().lower()
    if isinstance(rec, dict) and em in _person_emails(rec):
        return True
    _people_slim_status_index()   # refreshes _people_slim_cache (fail-soft)
    by_email = _people_slim_cache.get("by_email")
    if not by_email:
        return False              # no comparison possible -> mismatch
    return str(pid) in by_email.get(em, ())


def _pd_cookie(value, max_age):
    return f"{PD_COOKIE}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax"


def _pd_js_str(v):
    """JSON string literal safe inside an inline <script>."""
    return (json.dumps(str(v)).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _pd_render_popup(token, deal_id, banner_html=""):
    """Overlay + script appended before </body>. Shown after 5s; no close."""
    book_href = PD_FULL_BOOK_URL + (f"&gp={urllib.parse.quote(token, safe='')}" if token else "")
    esc = lambda u: html_mod.escape(u, quote=True)
    css = """
<style>
#pdOverlay{display:none;position:fixed;inset:0;z-index:2147483000;background:rgba(20,24,30,.55);
  align-items:center;justify-content:center;padding:16px;box-sizing:border-box;overflow-y:auto}
html.pd-on #pdOverlay{display:flex}
html.pd-on,html.pd-on body{overflow:hidden}
html.pd-on body>*:not(#pdOverlay):not(#pdPreviewBanner){filter:blur(8px);pointer-events:none;user-select:none}
#pdCard{background:#fff;border-radius:10px;max-width:440px;width:100%;box-sizing:border-box;
  padding:28px 26px 22px;box-shadow:0 12px 40px rgba(0,0,0,.3);font-family:inherit;color:var(--text,#222);text-align:center}
#pdCard h2{margin:0 0 12px;font-size:20px;line-height:1.3;color:var(--text,#222)}
#pdCard p{margin:0 0 20px;font-size:15px;line-height:1.5;color:var(--text-secondary,#555)}
#pdCard .pd-primary{display:block;padding:12px 16px;border-radius:6px;background:var(--accent,#3a5a75);
  color:#fff;font-weight:600;font-size:15px;text-decoration:none}
#pdCard .pd-primary:hover{filter:brightness(1.1)}
#pdCard .pd-label{margin:20px 0 8px;font-size:13px;font-weight:600;color:var(--text-secondary,#555)}
#pdCard .pd-row{display:flex;gap:10px}
#pdCard .pd-secondary{flex:1;padding:10px 12px;border-radius:6px;border:1px solid var(--accent,#3a5a75);
  color:var(--accent,#3a5a75);background:#fff;font-weight:600;font-size:14px;text-decoration:none}
#pdCard .pd-secondary:hover{background:#f3f6f9}
#pdCard .pd-leave{display:inline-block;margin-top:18px;font-size:12px;color:var(--text-secondary,#777);text-decoration:underline}
#pdPreviewBanner{position:fixed;top:0;left:0;right:0;z-index:2147483001;background:#fff4c2;color:#5a4500;
  border-bottom:1px solid #e0c96a;font:12px/1.4 monospace;padding:6px 16px;word-break:break-word}
</style>"""
    html_part = (
        banner_html
        + '<div id="pdOverlay" role="dialog" aria-modal="true" aria-labelledby="pdTitle">'
        '<div id="pdCard">'
        '<h2 id="pdTitle">This deal is not available to co-brokers or foreign finders.</h2>'
        "<p>Our records show you as an intermediary. If you&rsquo;re investing directly, "
        "complete Rainmaker&rsquo;s investor qualification form and we&rsquo;ll update your record.</p>"
        f'<a class="pd-primary" data-pd="partner_desk" href="{esc(book_href)}">See how to access the full book</a>'
        '<div class="pd-label">I&rsquo;m a direct investor</div>'
        '<div class="pd-row">'
        f'<a class="pd-secondary" data-pd="iqf_individual" href="{esc(PD_IQF_INDIVIDUAL_URL)}" target="_blank" rel="noopener">Individual</a>'
        f'<a class="pd-secondary" data-pd="iqf_entity" href="{esc(PD_IQF_ENTITY_URL)}" target="_blank" rel="noopener">Entity</a>'
        '</div>'
        f'<a class="pd-leave" data-pd="leave" href="{esc(PD_LEAVE_URL)}">Leave</a>'
        '</div></div>'
    )
    js = """
<script>
(function(){
  var TOKEN = __TOKEN__, DID = __DID__, BEACON = __BEACON__;
  setTimeout(function(){ document.documentElement.classList.add('pd-on'); }, 5000);
  var links = document.querySelectorAll('#pdOverlay a[data-pd]');
  for (var i = 0; i < links.length; i++) {
    links[i].addEventListener('click', function(){
      if (!TOKEN) return;
      try { navigator.sendBeacon(BEACON, JSON.stringify({gp: TOKEN, action: this.getAttribute('data-pd'), did: DID})); } catch (e) {}
    });
  }
})();
</script>"""
    js = (js.replace("__TOKEN__", _pd_js_str(token or ""))
            .replace("__DID__", _pd_js_str(deal_id))
            .replace("__BEACON__", _pd_js_str(PD_BEACON_URL)))
    return css + html_part + js


def _pd_preview_banner():
    secret, s_err = _pd_load_secret()
    people, a_err = _pd_load_access()
    s_txt = "OK" if secret else f"FAIL ({s_err})"
    a_txt = f"OK ({len(people)} people)" if people is not None else f"FAIL ({a_err})"
    return ('<div id="pdPreviewBanner">PD preview &mdash; secret: '
            + html_mod.escape(s_txt) + ', access.json: ' + html_mod.escape(a_txt) + '</div>')


def _pd_decide(event, qp):
    """None for a normal visitor, else {"popup": bool, "token": str,
    "cookie": Set-Cookie value or None, "banner": html}."""
    if (qp.get('pd_preview') == '1' and ADMIN_KEY and qp.get('key')
            and hmac.compare_digest(qp['key'].encode(), ADMIN_KEY.encode())):
        return {"popup": True, "token": "", "cookie": None, "banner": _pd_preview_banner()}

    q_token = qp.get('gp')
    c_token = _get_cookie(event, PD_COOKIE)
    token = q_token or c_token
    if not token:
        return None
    secret, err = _pd_load_secret()
    if not secret:
        logger.warning(f"pd: secret unavailable: {err}")
        return None
    pid, err = _pd_verify_token(token, secret)
    if not pid:
        logger.info(f"pd: {err} ({'query' if q_token else 'cookie'})")
        return None

    email = _read_identity_email(event)
    people, a_err = (None, None)
    if email:
        people, a_err = _pd_load_access()
        rec = (people or {}).get(pid)
        if not _pd_gg_id_matches(email, pid, rec):
            logger.info("pd: token/gg_id mismatch")
            if c_token:
                return {"popup": False, "token": "", "cookie": _pd_cookie("", 0), "banner": ""}
            return None

    if people is None:
        people, a_err = _pd_load_access()
    if people is None:
        logger.warning(f"pd: access.json unavailable: {a_err}")
        return None
    rec = people.get(pid)
    if not _pd_is_intermediary(rec):
        logger.info("pd: not shown (" + ("not in access.json" if rec is None else "not an eligible intermediary") + ")")
        return None
    set_cookie = _pd_cookie(token, PD_COOKIE_MAX_AGE) if q_token else None
    return {"popup": True, "token": token, "cookie": set_cookie, "banner": ""}


def _pd_apply(event, qp, deal_id, response):
    """The response, plus the pop-up / gg_pd cookie when warranted. Any error
    returns the original response untouched."""
    try:
        d = _pd_decide(event, qp)
        if not d or (not d["popup"] and not d["cookie"]):
            return response
        out = dict(response)
        headers = dict(response.get('headers') or {})
        headers['Cache-Control'] = 'private, no-store'
        if d["popup"]:
            body = response['body']
            i = body.rfind('</body>')
            if i < 0:
                return response
            out['body'] = body[:i] + _pd_render_popup(d["token"], deal_id, d["banner"]) + body[i:]
            logger.info("pd: pop-up shown" + (" (preview)" if d["banner"] else ""))
        if d["cookie"]:
            if event.get('version') == '2.0' or 'rawPath' in event or 'cookies' in event:
                out['cookies'] = [d["cookie"]]
            else:
                headers['Set-Cookie'] = d["cookie"]
        out['headers'] = headers
        return out
    except Exception as e:
        logger.warning(f"pd: failed, normal page served: {type(e).__name__}: {e}")
        return response


# Top nav, deal switcher and header/logo CSS. Shared by the full deal page and
# the Private-deal page (render_private_deal_page); injected into their f-strings.
PAGE_NAV_CSS = """.topnav {
                display: flex;
                align-items: center;
                flex-wrap: nowrap;
                gap: 12px;
                padding: 10px 0;
                margin-bottom: 10px;
                border-bottom: 1px solid #ddd;
            }
            .nav-brand {
                display: inline-block;
                background-color: #eef2f6;
                border: 1px solid #d7dee6;
                border-radius: 999px;
                padding: 7px 11px;
                font-size: 13.5px;
                font-weight: 700;
                color: var(--accent, #3d5a73);
                text-decoration: none;
                white-space: nowrap;
                flex-shrink: 0;
            }
            .nav-tabs {
                display: flex;
                align-items: center;
                flex-wrap: nowrap;
                gap: 6px;
                flex: 1;
                min-width: 0;
            }
            .nav-tab {
                display: inline-block;
                background-color: #fff;
                border: 1px solid #ddd;
                border-radius: 999px;
                padding: 7px 11px;
                font-size: 13.5px;
                font-weight: 600;
                color: var(--ink);
                text-decoration: none;
                white-space: nowrap;
            }
            /* Allow wrapping below 1080px. The one-row nav (admin search icon + 2-digit auction count) needs ~954px, so it wraps only when it must. */
            @media (max-width: 1080px) {
                .topnav {
                    flex-wrap: wrap;
                }
                .nav-tabs {
                    flex-wrap: wrap;
                    min-width: auto;
                }
            }
            .nav-tab:hover {
                background-color: #f0f0f0;
            }
            .nav-tab-active,
            .nav-tab-active:hover {
                background-color: #1a1a1a;
                border-color: #1a1a1a;
                color: #fff;
            }
            .nav-tab-disabled {
                color: #999;
                cursor: default;
            }
            .nav-tab-disabled:hover {
                background-color: #fff;
            }
            .btn.nav-signin {
                background-color: #fff;
                color: var(--ink);
                border: 1px solid #ddd;
                border-radius: 999px;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: 600;
                white-space: nowrap;
                margin-bottom: 0;
                margin-left: auto;
            }
            .btn.nav-signin:hover {
                background-color: #f0f0f0;
            }
            .nav-icon-btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 34px;
                height: 34px;
                background-color: #fff;
                border: 1px solid #ddd;
                border-radius: 999px;
                color: var(--ink);
                cursor: pointer;
                padding: 0;
            }
            .nav-icon-btn:hover {
                background-color: #f0f0f0;
            }
            .deal-switcher-overlay {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(0,0,0,0.5);
                z-index: 2000;
                padding-top: 12vh;
            }
            .deal-switcher-overlay.show {
                display: block;
            }
            .deal-switcher-box {
                background-color: #fff;
                margin: 0 auto;
                width: 90%;
                max-width: 560px;
                border-radius: 8px;
                box-shadow: 0 8px 30px rgba(0,0,0,0.25);
                overflow: hidden;
                font-family: Arial, sans-serif;
            }
            .deal-switcher-input {
                width: 100%;
                box-sizing: border-box;
                border: none;
                border-bottom: 1px solid #ddd;
                padding: 16px 18px;
                font-size: 16px;
                font-family: inherit;
                outline: none;
            }
            .deal-switcher-results {
                max-height: 50vh;
                overflow-y: auto;
            }
            .deal-switcher-row {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                padding: 10px 18px;
                cursor: pointer;
                font-size: 14px;
                color: #333;
            }
            .deal-switcher-row.active {
                background-color: #f0f4f8;
            }
            .deal-switcher-label {
                flex: 1;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .deal-switcher-copy {
                flex-shrink: 0;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 26px;
                height: 26px;
                background: none;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                color: #666;
                padding: 0;
            }
            .deal-switcher-copy:hover {
                background-color: #e2e6ea;
                color: #333;
            }
            .deal-switcher-copy svg {
                width: 14px;
                height: 14px;
                fill: none;
                stroke: currentColor;
                stroke-width: 1.3;
            }
            .deal-switcher-empty {
                padding: 16px 18px;
                color: #888;
                font-size: 14px;
            }
            .navacct {
                position: relative;
                margin-left: auto;
            }
            .navacct-trigger {
                display: inline-block;
                background-color: #fff;
                border: 1px solid #ddd;
                border-radius: 999px;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: 600;
                color: var(--ink);
                cursor: pointer;
                white-space: nowrap;
            }
            .navacct-trigger:hover {
                background-color: #f0f0f0;
            }
            .navacct-menu {
                display: none;
                position: absolute;
                right: 0;
                top: 100%;
                margin-top: 6px;
                background: #fff;
                border-radius: 6px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                min-width: 220px;
                padding: 6px 0;
                z-index: 50;
            }
            .navacct:hover .navacct-menu, .navacct:focus-within .navacct-menu {
                display: block;
            }
            .navacct-item {
                display: block;
                padding: 9px 16px;
                font-size: 13px;
                color: var(--ink);
                text-decoration: none;
                white-space: nowrap;
            }
            .navacct-item:hover {
                background: #f4f4f4;
            }
            .navacct-static {
                color: var(--text-secondary, #666);
                font-weight: 600;
                cursor: default;
            }
            .navacct-static:hover {
                background: none;
            }
            .navacct-disabled {
                color: #999;
                cursor: default;
            }
            .navacct-disabled:hover {
                background: none;
            }
            .header {
                flex-wrap: wrap;
            }
            .header-content {
                flex: 1;
                min-width: 300px;
            }
            .logo-container {
                margin: 0 20px;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            .company-logo {
                max-width: 120px;
                max-height: 80px;
                object-fit: contain;
            }"""


GA_HEAD_HTML = (
    '<script async src="https://www.googletagmanager.com/gtag/js?id=G-L9JN3TRR2S"></script>'
    '<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}'
    "gtag('js',new Date());gtag('config','G-L9JN3TRR2S');</script>")

ROBOTS_NOINDEX_HTML = '<meta name="robots" content="noindex">'
VIS_UNAVAILABLE_TEXT = "This deal is not currently available."


def render_visibility_banner(visibility):
    """Admin-only label for a Private / Interested-only deal; '' otherwise."""
    if visibility == VIS_PRIVATE:
        text, color, bg = "Private: not shown to counterparties", "#b91c1c", "#fdf2f2"
    elif visibility == VIS_INTERESTED:
        text, color, bg = "Interested investors only", "#b45309", "#fdf6e7"
    else:
        return ''
    return (f'<div class="vis-banner" style="margin:12px 0;padding:8px 12px;'
            f'border:1px solid {color};border-left:4px solid {color};border-radius:6px;'
            f'background:{bg};color:{color};font-weight:600;font-size:14px">{text}</div>')


def render_private_deal_page(company_name, top_nav_html, deal_switcher_html, similar_html=''):
    """Stub page for a Private deal seen by a non-admin: company header, one
    unavailable line and the search/similar links. Carries no deal terms,
    price, size, structure, fees, notes, Q&A, bid or LOI controls."""
    co = html_mod.escape(str(company_name or ''), quote=True)
    logo = "https://bannerlogos.s3.us-east-1.amazonaws.com/" + re.sub(r'[^a-zA-Z0-9]', '', str(company_name or '')) + ".png"
    title = f"{co} | Gracia Group" if co else "Gracia Group"
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        {GA_HEAD_HTML}
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {ROBOTS_NOINDEX_HTML}
        <title>{title}</title>
        <link rel="stylesheet" href="https://s3.us-east-1.amazonaws.com/main.css/master.css">
        <style>
            {PAGE_NAV_CSS}
            .bid-btn {{ background-color: #3d7355; color: #fff; }}
            .bid-btn:hover {{ background-color: #345f48; }}
            .similar-box {{ max-width:420px; box-sizing:border-box; border:1px solid var(--border-strong);
                            border-radius:8px; padding:14px 16px; background:#faf8f3; font-size:13px; }}
            .similar-box h2 {{ margin:0 0 10px 0; font-size:15px; }}
            .similar-pills {{ display:flex; flex-wrap:wrap; gap:6px; }}
            .similar-pill {{ display:inline-block; background:#CCDBEA; border:none;
                             color:var(--ink); font-weight:500; font-size:14px; padding:6px 14px;
                             border-radius:999px; text-decoration:none; }}
            .similar-pill:hover {{ background:#B7CBE1; }}
            .similar-note {{ margin:10px 0 0; font-size:11px; color:var(--text-secondary); }}
            .similar-count {{ font-size:10px; font-weight:600; margin-left:4px; color:inherit; opacity:0.65; }}
        </style>
    </head>
    <body>
        {top_nav_html}
        <div class="header">
            <div class="header-content">
                <h1><strong>{co}</strong></h1>
            </div>
            <div class="logo-container">
                <img src="{logo}" alt="{co} logo" class="company-logo"
                     onerror="this.src='https://bannerlogos.s3.us-east-1.amazonaws.com/default.png';">
            </div>
        </div>
        <div class="closed-banner" style="{ALT_BOX_STYLE}">
            <h2 style="margin:0 0 16px;font-size:20px">{VIS_UNAVAILABLE_TEXT}</h2>
            <div style="display:flex;flex-wrap:wrap;gap:12px">
                <a href="#" class="btn bid-btn" onclick="var b=document.getElementById('dealSwitcherBtn');if(b){{b.click();}}return false;">Search similar opportunities</a>
                <a href="https://trades.graciagroup.com/" class="btn">View live indications</a>
            </div>
        </div>
        {similar_html}
        {deal_switcher_html}
    </body>
    </html>
    """


def lambda_handler(event, context):
    logger.info("Lambda function started")
    logger.info(f"Incoming event: {json.dumps(dg_redacted_event(event), default=str)}")

    deal_id = event.get('queryStringParameters', {}).get('deal_id')
    
    # If not found in query parameters, try path parameters
    if not deal_id and event.get('pathParameters'):
        deal_id = event.get('pathParameters', {}).get('deal_id')
        
    if not deal_id:
        return {'statusCode': 400, 'body': json.dumps({"error": "deal_id is required"})}
    
    logger.info(f"Extracted deal_id: {deal_id}")

    _qp = event.get('queryStringParameters') or {}
    # Personal view-only link (?v=): set gg_view and 302 to the same URL without v.
    _dg_redirect = dg_link_redirect(event, f"/deal/{deal_id}")
    if _dg_redirect:
        return _dg_redirect
    # Fail closed: with no ADMIN_KEY configured, nobody is admin.
    _is_admin = bool(ADMIN_KEY) and any(
        v and hmac.compare_digest(v.encode(), ADMIN_KEY.encode())
        for v in (_qp.get('admin_key'), _get_cookie(event, 'admin_key')))

    # Deal gate. A valid partner-desk ?gp= token also sets gg_view for its pid
    # (view-only; partner-desk behaviour itself is untouched), only while the
    # switch is ON so switched-off traffic is unchanged.
    _dg_switch = dg_switch_enabled()
    _dg_gp_pid, _dg_cookies = None, []
    if _dg_switch and _qp.get('gp') and _dg_secret():
        try:
            _dg_pd_secret, _ = _pd_load_secret()
            if _dg_pd_secret:
                _dg_gp_pid, _ = _pd_verify_token(_qp.get('gp'), _dg_pd_secret)
                if _dg_gp_pid:
                    _dg_cookies.append(dg_view_cookie(_dg_gp_pid))
        except Exception as e:
            logger.warning(f"deal-gate: gp check failed: {type(e).__name__}")
            _dg_gp_pid = None
    dg_gated, dg_state, dg_preview = dg_decide(event, _qp, _is_admin, _dg_gp_pid)
    if dg_preview:
        _is_admin = False   # admin preview renders the page exactly as that viewer
    if dg_gated:
        logger.info(f"deal-gate: terms withheld ({dg_state})")

    def _dg_finish(resp):
        """gg_view from ?gp= and no-store caching while the gate can vary the page."""
        resp = dg_add_cookies(resp, event, _dg_cookies)
        if _dg_switch or dg_preview:
            out = dict(resp)
            out['headers'] = dict(resp.get('headers') or {})
            out['headers']['Cache-Control'] = 'private, no-store'
            return out
        return resp
    top_nav_html = _render_top_nav(event, _is_admin, active='indications')
    deal_switcher_html = _render_deal_switcher_modal(_is_admin)

    deal_data = fetch_deal_data(deal_id)
    
    if not deal_data:
        return {'statusCode': 404, 'body': json.dumps({"error": "Deal not found"})}

    # Closed deals (Obsolete/Won/Lost) render a trimmed landing page for
    # non-admins; admins always get the full page.
    _early_stage = get_deal_stage_name(deal_data)
    is_closed_public = (_early_stage.lower() in CLOSED_STAGES) and not _is_admin
    
    custom_fields = deal_data.get('custom_fields', {})
    mapped_fields = map_custom_fields(custom_fields)
    
    deal_name = deal_data.get('name', 'Unknown Deal')
    
    # Extract company data
    company_data = deal_data.get('company_data', {})
    # Extract company name
    company_name = company_data.get('name', 'Unknown Company')

    # Visibility: the live Pipeline value on the deal just fetched (fresher
    # than deals.json / visibility-overrides.json, so no override lookup here).
    visibility = deal_visibility(custom_fields)
    if visibility == VIS_PRIVATE and not _is_admin:
        _priv_type = map_option_value('Type', mapped_fields.get('Type', []))
        try:
            _priv_similar = render_similar_companies(company_name, _priv_type, deal_id)
        except Exception as e:
            logger.warning(f"visibility: similar companies failed on private page: {type(e).__name__}")
            _priv_similar = ''
        return _dg_finish({
            'statusCode': 200,
            'headers': {'Content-Type': 'text/html', 'X-Robots-Tag': 'noindex'},
            'body': render_private_deal_page(company_name, top_nav_html, deal_switcher_html, _priv_similar),
        })
    robots_meta_html = ROBOTS_NOINDEX_HTML if visibility in (VIS_PRIVATE, VIS_INTERESTED) else ''
    visibility_banner_html = render_visibility_banner(visibility) if _is_admin else ''

    # Fetch news data for the company - using exact phrase matching with business context
    business_context = "funding OR pre-IPO OR finance OR investment OR technology OR startup OR venture OR company OR business"
    news_html = ""

    # Skip the News section for companies whose search returns mostly junk (see NEWS_SKIP_COMPANIES)
    news_data = None
    if is_closed_public:
        pass
    elif company_name.strip().lower() in NEWS_SKIP_COMPANIES:
        logger.info(f"News disabled for company: {company_name}")
    else:
        news_data = test_news_api(f'"{company_name}" AND ({business_context})')

    # Process news data if available
    if news_data and news_data.get('articles'):
        articles = news_data.get('articles', [])[:5]  # Limit to 5 articles
        
        news_html = "<h2>Recent News</h2><ul>"
        
        for article in articles:
            title = article.get('title', 'No Title')
            source = article.get('source', {}).get('name', 'Unknown Source')
            url = article.get('url', '#')
            published_date = article.get('publishedAt', '')
            
            # Format date if available
            formatted_date = published_date
            if published_date:
                try:
                    date_obj = datetime.strptime(published_date, '%Y-%m-%dT%H:%M:%SZ')
                    formatted_date = date_obj.strftime('%b %d, %Y')
                except ValueError:
                    formatted_date = published_date
            
            news_html += f"""
            <li>
                <a href="{url}" target="_blank">{title}</a>
                <div style="font-size: 0.9em; color: #666;">
                    {source} • {formatted_date}
                </div>
            </li>
            """
        
        news_html += "</ul>"

    # Continue with the rest of your code
    company_custom_fields = company_data.get('custom_fields', {})
    
    # Map company custom fields for valuation calculation
    company_lr_pps = company_custom_fields.get('custom_label_3064363', '')  # Company Last Round PPS
    company_lr_val = company_custom_fields.get('custom_label_3790429', '')  # Company Last Round Valuation
    
    # Calculate estimated valuations and price comparisons
    gross_price = mapped_fields.get('Gross', '')
    net_price = mapped_fields.get('Net', '')
    shares = mapped_fields.get('Shares', '')

    gross_valuation = calculate_estimated_valuation(gross_price, shares, company_lr_val, company_lr_pps)
    net_valuation = calculate_estimated_valuation(net_price, shares, company_lr_val, company_lr_pps)

    # Calculate price comparisons
    gross_comparison = calculate_price_comparison(gross_price, company_lr_pps)
    net_comparison = calculate_price_comparison(net_price, company_lr_pps)

    gross_with_valuation = format_price_with_comparison(gross_price, gross_valuation, gross_comparison)
    net_with_valuation = format_price_with_comparison(net_price, net_valuation, net_comparison)

    # Empty/zero price handling: zero counts as no price. Sell orders with no
    # net and no gross solicit a bid; buy orders solicit an offer; any other
    # empty/zero price cell renders as a dash.
    _sol_type = map_option_value('Type', mapped_fields.get('Type', []))
    def _price_empty(v):
        if v is None or str(v).strip() in ('', 'None'):
            return True
        try:
            return float(v) == 0
        except (ValueError, TypeError):
            return False
    try:
        _sol_seller_gp = int(float(str(mapped_fields.get('Seller Type', '') or 0))) == 7020357
    except (ValueError, TypeError):
        _sol_seller_gp = False
    # Seller's Est. Valuation (billions): blank = none, exactly 0 = seller said unknown.
    _est_val_raw = (deal_data.get('custom_fields') or {}).get('custom_label_4009563')
    if isinstance(_est_val_raw, list):
        _est_val_raw = _est_val_raw[0] if _est_val_raw else None
    try:
        _est_val = None if _est_val_raw is None or str(_est_val_raw).strip() in ('', 'None') else float(str(_est_val_raw).replace(',', ''))
    except (ValueError, TypeError):
        _est_val = None
    _est_val_pos = _est_val is not None and _est_val > 0
    _price_val_shown = ((not _price_empty(gross_price) and gross_valuation is not None)
                        or (not _price_empty(net_price) and net_valuation is not None))
    est_val_row = None
    if _sol_type == "Sell Order" and _sol_seller_gp and _est_val_pos:
        est_val_row = ("Round valuation", f"{format_tilde_valuation(_est_val)} pre-money")
    elif _sol_type == "Sell Order" and not _sol_seller_gp and not _price_val_shown and _est_val is not None:
        if _est_val_pos:
            est_val_row = ("Est. Valuation", f"{format_tilde_valuation(_est_val)} (seller&rsquo;s estimate)")
        elif _est_val == 0:
            est_val_row = ("Est. Valuation", "Not provided or unknown")

    if _price_empty(gross_price) and _price_empty(net_price):
        if _sol_type == "Sell Order" and _sol_seller_gp and _est_val_pos:
            net_with_valuation = f"Tied to round at {format_tilde_valuation(_est_val)} pre-money"
            gross_with_valuation = "-"
        elif _sol_type == "Sell Order" and _sol_seller_gp:
            net_with_valuation = "Price tied to upcoming round"
            gross_with_valuation = "-"
        elif _sol_type == "Sell Order" and _est_val_pos:
            net_with_valuation = f"Priced at {format_tilde_valuation(_est_val)} valuation"
            gross_with_valuation = "-"
        elif _sol_type == "Sell Order":
            gross_with_valuation = "Make a bid"
            net_with_valuation = "-"
        elif _sol_type == "Buy Order":
            net_with_valuation = "Make an offer"
            gross_with_valuation = "-"
        else:
            gross_with_valuation = "-"
            net_with_valuation = "-"
    else:
        if _price_empty(gross_price):
            gross_with_valuation = "-"
        if _price_empty(net_price):
            net_with_valuation = "-"

    # Get company summary separately
    company_summary = company_data.get('description', '')

    # Recent development (catalyst) the scanner writes to the company record
    company_catalyst = company_custom_fields.get('custom_label_3999603', '')
    catalyst_html = ""
    if company_catalyst and str(company_catalyst).strip():
        catalyst_html = (
            f'<div class="catalyst">'
            f'<span class="catalyst-label">Recent Development</span>{company_catalyst}'
            f'</div>'
        )

    # Deal stage (Inquiry / Firm / Obsolete / ...) -- logged so the exact set of
    # stage names in use is visible in CloudWatch.
    stage_name = get_deal_stage_name(deal_data)
    logger.info(f"Deal stage: {stage_name or '(none)'} (raw: {deal_data.get('deal_stage')!r})")
    stage_html = render_stage_status(stage_name)
    if is_closed_public:
        stage_html = ""

    table_data = [
        ("Type", map_option_value('Type', mapped_fields.get('Type', []))),
        ("Class", map_option_value('Class', mapped_fields.get('Class', ''))),
        ("Net", net_with_valuation),
        ("Gross", gross_with_valuation),
        *([est_val_row] if est_val_row else []),
        ("Shares", "{:,.0f}".format(float(mapped_fields.get('Shares', 0))) if mapped_fields.get('Shares') is not None else ''),
        ("Company LR (PPS)", format_currency(company_lr_pps, include_cents=True)),
        ("Company LR Val ($Bn)", format_currency(company_lr_val, include_cents=True)),
        ("Min Deal Size", format_currency(mapped_fields.get('Min Deal Size', ''))),
        ("Max Deal Size", format_currency(mapped_fields.get('Max Deal Size', ''))),
        ("Notes", deal_data.get('summary', ''))
    ]
    if dg_gated:
        # Deal gate: fixed placeholders only; the real values never reach the page.
        table_data = [
            ("Type", map_option_value('Type', mapped_fields.get('Type', []))),
            ("Class", map_option_value('Class', mapped_fields.get('Class', ''))),
            ("Net", dg_ph(DG_PH_PRICE)),
            ("Gross", dg_ph(DG_PH_PRICE)),
            ("Shares", dg_ph(DG_PH_NUM)),
            ("Company LR (PPS)", format_currency(company_lr_pps, include_cents=True)),
            ("Company LR Val ($Bn)", format_currency(company_lr_val, include_cents=True)),
            ("Min Deal Size", dg_ph(DG_PH_SIZE)),
            ("Max Deal Size", dg_ph(DG_PH_SIZE)),
            ("Notes", dg_ph(DG_PH_TEXT)),
        ]

    _dr = (deal_data.get('custom_fields') or {}).get('custom_label_3952402')
    _dr = str(_dr) if _dr is not None else None
    DR_YES = '7038265'
    DR_NO  = '7038266'
    if _dr == DR_YES:
        data_room_display = 'Yes'
    elif _dr == DR_NO:
        data_room_display = 'No'
    else:                       # Confirm, null, or missing -> not definitive
        data_room_display = '?'  # unset
    ask_data_room = (data_room_display == '?')

    spv_fees = [
        ("Management Fee", format_percentage(mapped_fields.get('Management Fee', ''))),
        ("Carry", format_percentage(mapped_fields.get('Carry', ''))),
        ("Seller Fee", format_percentage(mapped_fields.get('Seller Fee', ''))),
        ("Partner Fee", format_percentage(mapped_fields.get('Partner Fee', '')))
    ]
    spv_details = [
        ("Layers", map_option_value('Layers', mapped_fields.get('Layers', ''))),
        ("Seller Role", map_option_value('Seller Type', mapped_fields.get('Seller Type', ''))),
        ("Price Status", map_option_value('Price Status', mapped_fields.get('Price Status', ''))),
        ("Data Room / VDR Available", data_room_display)
    ]
    if mapped_fields.get('Fund Exemption'):
        _fe_raw = str(mapped_fields.get('Fund Exemption', ''))
        _fe_label = map_option_value('Fund Exemption', _fe_raw)
        if _fe_raw == '7200027':
            # 3(c)(1): if the SPV charges carry, Rule 205-3 requires Qualified
            # Clients. Blank/unparseable carry defaults to the stricter label.
            try:
                _carry_val = float(str(mapped_fields.get('Carry', '')).replace('%', '').strip())
            except ValueError:
                _carry_val = None
            _fe_label = ('3(c)(1) - Accredited Investors ($1M+ net worth) &amp; Qualified Purchasers ($5M+ investments)' if _carry_val == 0
                         else '3(c)(1) - Qualified Clients ($2.2M+ net worth) &amp; Qualified Purchasers ($5M+ investments)')
        spv_details.append(("Fund Exemption", _fe_label))
    _final_deadline = ((deal_data.get('custom_fields') or {}).get('custom_label_4006402') or '')
    if _final_deadline:
        spv_details.insert(0, ("Deadline", str(_final_deadline)[:10].replace('/', '-')))
    spv_data = spv_details + spv_fees
    spv_split = len(spv_details)
    
    bid_button_text = "Offer" if map_option_value('Type', mapped_fields.get('Type', [])) == "Buy Order" else "Bid"

    # Bid/Offer destination: a live auction seeded from this deal wins over
    # web-bid. Any auctions.json read failure falls back to web-bid (see
    # live_auction_for_deal), so the page never breaks on this.
    _bid_side = 'sell' if bid_button_text == 'Offer' else 'buy'
    _web_bid_dest = (
        f"{DESK_URL}/bid/?name={urllib.parse.quote(company_name)}&side={_bid_side}"
        f"&deal_id={deal_id}"
        + (f"&px={urllib.parse.quote(str(gross_price))}" if gross_price else "")
    )
    _live_auction_id = None if is_closed_public else live_auction_for_deal(deal_id)
    bid_dest = (f"{DESK_URL}/?view=auction&id={urllib.parse.quote(str(_live_auction_id))}"
                if _live_auction_id else _web_bid_dest)
    if _live_auction_id:
        bid_button_text = "Bid — Auction in Progress"

    # Login-gated: a signed-in viewer (gg_id cookie) goes straight to bid_dest
    # with a fresh SSO handoff token. A signed-out viewer gets a modal whose
    # Sign In button round-trips through Cognito with bid_dest (no token) as
    # the bare `state` payload, then trades' code-exchange leg re-mints the
    # token and bounces back here to bid_dest itself.
    _bid_email = _read_identity_email(event)
    bid_modal_html = ""
    if is_closed_public:
        bid_button_html = ""
    elif _bid_email:
        _bid_token = _make_handoff_token(_bid_email)
        _bid_sep = '&' if '?' in bid_dest else '?'
        bid_href = f"{bid_dest}{_bid_sep}sso={urllib.parse.quote(_bid_token, safe='')}"
        bid_button_html = f'<a href="{bid_href}" class="btn bid-btn">{bid_button_text}</a>'
    else:
        _bid_state = base64.urlsafe_b64encode(bid_dest.encode()).decode().rstrip('=')
        _bid_cognito_host = "https://us-east-1dsttcaqx7.auth.us-east-1.amazoncognito.com"
        _bid_cognito_qs = (
            "?client_id=71vrglkidm13jb73u7nje3d1t2&response_type=code&scope=openid+email"
            "&redirect_uri=https://trades.graciagroup.com"
            f"&state={urllib.parse.quote(_bid_state, safe='')}"
        )
        _bid_cognito_url = f"{_bid_cognito_host}/login{_bid_cognito_qs}"
        _bid_signup_url = f"{_bid_cognito_host}/signup{_bid_cognito_qs}"
        bid_button_html = (
            '<a href="#" class="btn bid-btn" '
            "onclick=\"document.getElementById('bidLoginModal').style.display='flex';return false;\">"
            f'{bid_button_text}</a>'
        )
        bid_modal_html = (
            '<div id="bidLoginModal" class="bid-modal-overlay">'
            '<div class="bid-modal">'
            '<h3>Sign in to place your bid</h3>'
            "<p>We&rsquo;ll bring you right back to the bidding page.</p>"
            '<div class="bid-modal-actions">'
            f'<a href="{_bid_cognito_url}" class="btn bid-btn">Sign In</a>'
            + (f'<div class="bid-modal-signup">First time here? '
               f'<a href="{_bid_signup_url}">Create an account</a> '
               '&mdash; use the email where you receive our deal updates.</div>')
            + '<a href="#" class="bid-modal-cancel" '
            "onclick=\"document.getElementById('bidLoginModal').style.display='none';return false;\">"
            'Cancel</a>'
            '</div></div></div>'
        )

    deal_type = map_option_value('Type', mapped_fields.get('Type', []))
    # Transferability (display-only): unavailable direct sells keep the page,
    # description, table and email capture but lose the Bid button and show
    # an unavailable status; notes go on one line under the header.
    transfer_note_html = ""
    if not is_closed_public:
        _tr_unavail, _tr_notes = tr_page_state(
            deal_data.get('custom_fields') or {}, company_custom_fields,
            deal_type == "Buy Order")
        if _tr_unavail:
            stage_html = render_tr_unavailable_status()
            bid_button_html = ""
            bid_modal_html = ""
        transfer_note_html = render_tr_notes(_tr_notes)
    owner_iqf_yes = False
    if deal_type == "Buy Order" and not is_closed_public:
        owner_iqf_yes = fetch_person_iqf_yes((deal_data.get('primary_contact') or {}).get('id'))
    if not is_closed_public and deal_type in ("Sell Order", "Buy Order"):
        _linked_ids = _deal_linked_person_ids(deal_data)
        if _linked_ids:
            stage_html += render_onboarding_status(deal_type, _linked_ids, _people_slim_status_index())
    _is_unconfirmed, _last_confirmed = unconfirmed_status(deal_data)
    qa_box_html = "" if (is_closed_public or dg_gated) else render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room, owner_iqf_yes, est_val=_est_val, unconfirmed=_is_unconfirmed)
    _msg_raw = (deal_data.get('custom_fields') or {}).get('custom_label_4001285')
    hide_questions = (str(_msg_raw) == '7187011')
    unconfirmed_html = ""
    if _is_unconfirmed:
        _show_ask = (MESSAGING_ALLOW in _cf_option_ids(_msg_raw)
                     and 'name="q_still_active"' in qa_box_html)
        unconfirmed_html = render_unconfirmed_notice(_last_confirmed, deal_type, _show_ask)
    similar_html = render_similar_companies(company_name, deal_type, deal_id)
    weekly_signup_html = "" if is_closed_public else render_weekly_signup(
        deal_id, company_name if dg_gated else deal_name)
    _side_inner = ('' if hide_questions else qa_box_html) + similar_html
    side_col_html = '<div class="side-col">' + _side_inner + '</div>' if _side_inner.strip() else ''

    def generate_table_html(data, split=None):
        mid = len(data) // 2 if split is None else split
        left_column = data[:mid]
        right_column = data[mid:]

        table_html = "<table>"
        for i in range(max(len(left_column), len(right_column))):
            table_html += "<tr>"
            if i < len(left_column):
                table_html += f"<th>{left_column[i][0]}</th><td>{left_column[i][1]}</td>"
            else:
                table_html += "<th></th><td></td>"
            table_html += "<td class='separator'></td>"
            if i < len(right_column):
                table_html += f"<th>{right_column[i][0]}</th><td>{right_column[i][1]}</td>"
            else:
                table_html += "<th></th><td></td>"
            table_html += "</tr>"
        table_html += "</table>"
        return table_html   

    def get_company_logo_url(company_name):
        """Generate company logo URL while preserving the correct case."""
        if not company_name:
            return None
        
        # Remove special characters but PRESERVE case
        import re
        normalized_name = re.sub(r'[^a-zA-Z0-9]', '', company_name)  # Remove special characters

        logo_url = f"https://bannerlogos.s3.us-east-1.amazonaws.com/{normalized_name}.png"
        
        return logo_url


    # Closed public pages keep the full historical terms; weekly signup and
    # news are already '' for them.
    deal_main_inner = f"""{generate_table_html(table_data)}

{'' if dg_gated else f"""        <div id="spvSection" style="display: {'' if map_option_value('Structure', mapped_fields.get('Structure', [])) == 'Fund' else 'none'}">
        <h2>SPV Details&thinsp;*</h2>
        {generate_table_html(spv_data, spv_split)}
        <p style="font-size:13.5px;color:#555;margin:10px 0 0 2px;font-style:italic">* Fees shown are the seller&rsquo;s SPV terms only and do not include broker commission, which is quoted separately.</p>
        </div>
"""}{weekly_signup_html}
        <!-- News Section -->
        <div id="newsSection" class="news-section">
            {news_html}
        </div>
"""

    # Other live offers in the same company, for non-live stages.
    alt_live_html = ""
    _alt_stage = get_deal_stage_name(deal_data).strip().lower()
    if _alt_stage and _alt_stage not in LIVE_STAGES:
        try:
            _alt_deals = _sim_load_deals()
        except Exception as e:
            logger.warning(f"alt-live: could not load deals: {e}")
            _alt_deals = []
        _alt_struct = map_option_value('Structure', mapped_fields.get('Structure', []))
        if isinstance(_alt_struct, (list, tuple)):
            _alt_struct = ', '.join(str(x) for x in _alt_struct)
        _alt_type = map_option_value('Type', mapped_fields.get('Type', []))
        _alts = live_alternatives(_alt_deals, deal_id, company_name, _alt_type, str(_alt_struct or ''),
                                  mapped_fields.get('Min Deal Size'), mapped_fields.get('Max Deal Size'))
        alt_live_html = render_alt_live_box(_alt_stage, is_closed_public, _alts,
                                            _seo_company(deal_name, company_name) or company_name, _alt_type,
                                            gated=dg_gated)

    canonical_html = (f'<link rel="canonical" href="https://trades.graciagroup.com/deal/{deal_id}">'
                      if str(deal_id).isdigit() else '')

    _seo_type = map_option_value('Type', mapped_fields.get('Type', []))
    _seo_structure = map_option_value('Structure', mapped_fields.get('Structure', []))
    seo_title = build_seo_title(deal_name, company_name, str(_seo_type or ''), str(_seo_structure or ''), custom_fields.get('custom_label_3064333'))
    seo_title_html = html_mod.escape(seo_title, quote=True) if seo_title else deal_name
    seo_desc = build_seo_description(deal_name, company_name, str(_seo_type or ''), str(_seo_structure or ''), custom_fields.get('custom_label_3064333'), mapped_fields.get('Min Deal Size'), is_closed_public)
    seo_desc_meta = f'<meta name="description" content="{html_mod.escape(seo_desc, quote=True)}">' if seo_desc else ''
    _struct_desc = get_structure_description(map_option_value('Structure', mapped_fields.get('Structure', [])))
    if seo_title is not None:
        _dn = str(deal_name or '')
        _sub = _dn.split(':', 1)[1].strip() if ':' in _dn else _dn.strip()
        if _struct_desc:
            _sd = _struct_desc[3:] if _struct_desc.startswith(' - ') else _struct_desc
            _sub = f'{_sub} · {_sd}' if _sub else _sd
        h1_html = (f'<h1><strong>{seo_title_html}</strong></h1>\n'
                   f'                <div class="deal-subline">{html_mod.escape(_sub, quote=True)}</div>')
    else:
        h1_html = f'<h1><strong>{deal_name}{_struct_desc}</strong></h1>'

    if dg_gated:
        # Title, description and H1 carry the company and Buy/Sell only.
        _dg_comp = _seo_company(deal_name, company_name) or str(company_name or '')
        _dg_side = {'Buy Order': 'Buy', 'Sell Order': 'Sell'}.get(str(_seo_type or ''), '')
        _dg_title = f"{_dg_comp} Pre-IPO Stock" + (f" – {_dg_side}" if _dg_side else '')
        seo_title_html = html_mod.escape(_dg_title, quote=True)
        if is_closed_public:
            _dg_desc = f'This {_dg_comp} allocation has closed. See current pre-IPO offers in similar companies.'
        else:
            _dg_desc = (f"{_dg_side} order for {_dg_comp} pre-IPO stock." if _dg_side
                        else f"{_dg_comp} pre-IPO stock.")
        seo_desc_meta = f'<meta name="description" content="{html_mod.escape(_dg_desc, quote=True)}">'
        h1_html = f'<h1><strong>{seo_title_html}</strong></h1>'
        # Bid/Offer button and its modal are replaced by the gate box.
        bid_button_html = ""
        bid_modal_html = dg_gate_box(dg_state, DG_SITE + dg_current_path(event, f"/deal/{deal_id}"))
    dg_admin_box_html = DG_ADMIN_BOX if _is_admin else ''

    ga_head_html = '' if _is_admin else GA_HEAD_HTML

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        {ga_head_html}
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {robots_meta_html}<title>{seo_title_html}</title>
        {seo_desc_meta}
        {canonical_html}
        <link rel="stylesheet" href="https://s3.us-east-1.amazonaws.com/main.css/master.css">
        <style>
            /* Page-specific layout only. The shared Gracia look (font stack,
               page container, header, buttons, tables, news list, disclaimer)
               comes from master.css. */
            {PAGE_NAV_CSS}
            th {{
                width: 20%;
            }}
            td {{
                width: 30%;
            }}
            .separator {{
                width: 1px;
                background-color: var(--border-strong);
                padding: 0;
            }}
            .price-comparison {{
                display: inline-block;
                white-space: nowrap;
            }}
            .price-comparison span {{
                font-size: 65%;
            }}
            .company-summary {{
                font-size: 1.2em;
                font-style: italic;
                color: var(--text-secondary);
                margin: 15px 0 20px 0;
                line-height: 1.6;
            }}
            .catalyst {{
                background-color: #faf8f3;
                border-left: 4px solid var(--accent);
                padding: 10px 16px;
                margin: 0 0 20px 0;
                font-size: 1.05em;
                color: var(--text);
            }}
            .catalyst-label {{
                display: block;
                font-size: 0.72em;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: var(--accent);
                font-weight: bold;
                margin-bottom: 3px;
            }}
            /* Deal stage, inline in the header metadata line. Green (--pos) and
               red (--neg) come from master.css; the palette has no amber, so
               --stage-amber is the one added colour, mixed to sit at the same
               muted depth as --pos / --neg rather than a bright warning yellow. */
            :root {{
                --stage-amber: #8b6423;
            }}
            .deal-status {{
                margin-left: 6px;
                white-space: nowrap;
            }}
            .deal-status span {{
                font-weight: 600;
            }}
            .status-firm     {{ color: var(--pos); }}
            .status-inquiry  {{ color: var(--stage-amber); }}
            .status-obsolete {{ color: var(--neg); }}
            .status-neutral  {{ color: var(--text); }}
            .onboard-status {{ white-space: nowrap; }}
            .onboard-ok {{ color: var(--pos); font-weight: 600; }}
            .deal-body {{ display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap; }}
            .deal-main {{ flex:1; min-width:320px; }}
            .qa-box {{ width:300px; border:1px solid var(--border-strong); border-radius:8px;
                       padding:14px 16px; background:#faf8f3; font-size:13px; }}
            .qa-box h2 {{ margin:0 0 10px 0; font-size:15px; }}
            .side-col {{ width:300px; display:flex; flex-direction:column; gap:16px; }}
            .side-col .qa-box {{ width:100%; box-sizing:border-box; }}
            .similar-box {{ width:100%; box-sizing:border-box; border:1px solid var(--border-strong);
                            border-radius:8px; padding:14px 16px; background:#faf8f3; font-size:13px; }}
            .similar-box h2 {{ margin:0 0 10px 0; font-size:15px; }}
            .similar-pills {{ display:flex; flex-wrap:wrap; gap:6px; }}
            .similar-pill {{ display:inline-block; background:#CCDBEA; border:none;
                             color:var(--ink); font-weight:500; font-size:14px; padding:6px 14px;
                             border-radius:999px; text-decoration:none;
                             transition:background-color 0.15s, color 0.15s; }}
            .similar-pill:hover {{ background:#B7CBE1; }}
            .similar-note {{ margin:10px 0 0; font-size:11px; color:var(--text-secondary); }}
            .weekly-box {{ border:none; border-radius:8px;
                           padding:20px 24px; background:var(--accent, #3a5a75);
                           font-size:13px; margin-top:24px; display:flex;
                           align-items:center; gap:16px 24px; flex-wrap:wrap; }}
            .weekly-text {{ flex:1 1 300px; }}
            .weekly-box h2 {{ margin:0 0 4px 0; font-size:16px; color:#fff; }}
            .weekly-lead {{ margin:0; font-size:12.5px; color:rgba(255,255,255,0.85);
                            line-height:1.45; }}
            .weekly-form {{ display:flex; gap:8px; align-items:center; flex:1 1 280px; }}
            .weekly-form .qa-email {{ margin:0; flex:1; width:auto; border:none;
                                      border-radius:6px; padding:9px 12px; }}
            .weekly-form .qa-email:focus {{ outline:2px solid #fff; outline-offset:1px; }}
            .weekly-form .qa-send {{ margin:0; width:auto; padding:9px 18px;
                                     white-space:nowrap; flex:none; background:#fff;
                                     color:var(--accent, #3a5a75); }}
            .weekly-form .qa-send:hover {{ opacity:1; background:#f0ece3; }}
            .weekly-note {{ margin:0; font-size:11px; color:rgba(255,255,255,0.7);
                            flex-basis:100%; }}
            .weekly-hp {{ position:absolute !important; left:-9999px !important;
                          height:1px; width:1px; overflow:hidden; }}
            .similar-count {{ font-size:10px; font-weight:600; margin-left:4px;
                              color:inherit; opacity:0.65; }}
            .qa-row {{ display:flex; align-items:flex-start; gap:8px; padding:7px 0; border-bottom:1px solid var(--border-strong); cursor:pointer; }}
            .qa-row input[type=checkbox] {{ margin-top:3px; flex:none; }}
            .qa-bid {{ margin:2px 0 6px 26px; display:flex; gap:6px; }}
            .qa-bid input {{ width:50%; padding:5px; font-size:12px; box-sizing:border-box; }}
            .qa-bid-note {{ margin:0 0 6px 26px; font-size:12px; color:var(--text-secondary); }}
            .qa-fees {{ margin:2px 0 6px 26px; display:flex; gap:6px; }}
            .qa-fees label {{ flex:1; display:flex; flex-direction:column; gap:2px; font-size:11px; color:var(--text-secondary); }}
            .qa-fees input {{ width:100%; padding:5px; font-size:12px; box-sizing:border-box; }}
            .qa-email {{ width:100%; padding:7px; margin:8px 0 0; box-sizing:border-box; font-size:13px; }}
            .qa-send {{ width:100%; padding:9px; margin-top:16px; font-size:13px; font-weight:600; cursor:pointer; border:none; border-radius:6px; background:var(--accent); color:#fff; }}
            .qa-send:hover {{ opacity:0.9; }}
            .qa-row:last-child {{ border-bottom:none; }}
            .qa-row.no-line {{ border-bottom:none; }}
            .qa-fees, .qa-bid {{ border-bottom:1px solid var(--border-strong); padding-bottom:12px; }}
            .qa-q {{ color:var(--text); margin-bottom:5px; }}
            .qa-a {{ color:var(--text-secondary); }}
            .qa-ask {{ display:inline-block; padding:3px 12px; font-size:12px;
                       border:1px solid var(--border-strong); border-radius:4px;
                       color:var(--text-secondary); background:#fff; text-decoration:none; }}
            .qa-ask:hover {{ background:#f0ece3; }}
            @media (max-width:860px) {{ .qa-box {{ width:100%; }} }}
            .copy-id {{ display:inline-flex; vertical-align:middle; margin:0 4px; color:var(--text-secondary); cursor:pointer; }}
            .copy-id:hover {{ color:var(--accent); }}
            .copy-id.copied {{ color:#16a34a; }}
            .bid-btn {{
                background-color: #3d7355;
                color: #fff;
            }}
            .bid-btn:hover {{
                background-color: #345f48;
            }}
            .bid-modal-overlay {{
                display: none;
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.45);
                align-items: center;
                justify-content: center;
                z-index: 1000;
            }}
            .bid-modal {{
                background: #fff;
                border-radius: 8px;
                padding: 24px 28px;
                max-width: 360px;
                width: 90%;
                box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25);
            }}
            .bid-modal h3 {{
                margin: 0 0 10px;
                color: var(--text);
            }}
            .bid-modal p {{
                margin: 0 0 18px;
                color: var(--text-secondary);
                font-size: 14px;
            }}
            .bid-modal-actions {{
                display: flex;
                align-items: center;
                flex-wrap: wrap;
                gap: 16px;
            }}
            .bid-modal-signup {{
                flex-basis: 100%;
                margin-top: 10px;
                font-size: 13px;
                color: var(--text-secondary);
            }}
            .bid-modal-signup a {{
                color: var(--accent);
            }}
            .bid-modal-cancel {{
                color: var(--text-secondary);
                font-size: 14px;
                text-decoration: underline;
            }}
            .deal-subline {{ font-size: 15px; color: var(--text-secondary); margin: 2px 0 4px; }}
            .alt-live-list {{ margin: 0 0 12px; padding-left: 18px; }}
            .alt-live-list li {{ margin: 4px 0; }}
            .unconfirmed-notice {{ display:flex; align-items:center; gap:8px; margin:8px 0 4px;
                padding:6px 12px; background:#fff7e6; color:#b45309; border:1px solid #b45309;
                border-radius:6px; font-size:13px; line-height:1.4; }}
            .unconfirmed-clock {{ flex:none; cursor:help; }}
            .unconfirmed-text {{ flex:1; }}
            .unconfirmed-ask {{ flex:none; margin-left:auto; padding:3px 10px; font-size:12px;
                font-weight:600; color:#b45309; background:#fff; border:1px solid #b45309;
                border-radius:5px; cursor:pointer; }}
            .unconfirmed-ask:hover {{ background:#fdebc8; }}
            @media (max-width:600px) {{ .unconfirmed-notice {{ flex-wrap:wrap; }} }}
        </style>
    </head>
    <body>
        {top_nav_html}
        <div class="header">
            <div class="header-content">
                {h1_html}
                <div class="deal-id">Deal ID: {deal_id} <span class="copy-id" onclick="copyDealId('{deal_id}', this)" title="Copy Deal ID"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></span> (Updated: {format_date(deal_data.get('updated_at', ''))}){stage_html}</div>
                <script>
                function copyDealId(id, el) {{
                    navigator.clipboard.writeText(id).then(function() {{
                        el.classList.add('copied');
                        el.setAttribute('title', 'Copied!');
                        setTimeout(function() {{ el.classList.remove('copied'); el.setAttribute('title', 'Copy Deal ID'); }}, 1200);
                    }});
                }}
                </script>
            </div>
            <div class="logo-container">
                <img src="{get_company_logo_url(company_name)}" 
                     alt="{company_name} logo" 
                     class="company-logo" 
                     onerror="console.log('Logo failed to load: ' + this.src); this.src='https://bannerlogos.s3.us-east-1.amazonaws.com/default.png';">
            </div>
            <div class="button-group">
                {bid_button_html}
            </div>
        </div>
        {visibility_banner_html}{dg_admin_box_html}{transfer_note_html}
        {unconfirmed_html}
        {bid_modal_html}
        {alt_live_html}
        <div class="company-summary">{company_summary}</div>
        {catalyst_html}

        <div class="deal-body">
            <div class="deal-main">
{deal_main_inner}            </div>
            {side_col_html}
        </div>
        <hr>
        <div id="disclaimer">
            <p>DISCLOSURE: Chad Gracia ("Gracia") is a principal of The Gracia Group, LLC ("Gracia Group") and a registered agent of Rainmaker Securities, LLC ("RMS"). Gracia Group is a consulting firm and outside business activity of Gracia. Gracia Group is not affiliated with RMS. Rainmaker Securities, LLC ("RMS") is a FINRA registered broker-dealer and SIPC member. Find this broker-dealer and its agents on BrokerCheck. Our relationship summary can be found on the RMS website.</p>
            <p>RMS is engaged by its clients to make referrals to buyers or sellers of private securities ("Securities"). If such client closes a Securities transaction with a buyer or seller so referred, RMS is entitled to a success fee from the client. Such success fee may be in the form of cash or in warrants to purchase securities of the client or client's affiliate. RMS or RMS representatives may hold equity in its issuer clients or in the issuers of securities purchased or sold by the parties to a transaction.</p>
            <p>This communication is confidential and is addressed only to its intended recipient. This communication does not represent an offer or solicitation to buy or sell Securities. Such an offer must be made via definitive legal documentation by the seller of securities.</p>
            <p>Investments in the Securities are speculative and involve a high degree of risk. An investor in the Securities should have little to no need for liquidity in the foreseeable future and have sufficient finances to withstand the loss of the entire investment.</p>
            <p>RMS does not recommend the purchase or sale of Securities. Potential buyers or sellers of the Securities should seek professional counsel prior to entering into any transaction.</p>
            <p>Chad Gracia is a registered agent of Rainmaker Securities, LLC (“RMS”) and a principal of Gracia Group. RMS is a FINRA registered broker-dealer and SIPC member. Find RMS and its agents on BrokerCheck. The RMS relationship summary can be found on the RMS website.  RMS is not an affiliate of Gracia Group. All securities transactions conducted by Chad Gracia will be conducted via RMS.</p>
        </div> 
        {deal_switcher_html}
    </body>

    </html>
    """
    
    response = {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/html'},
        'body': html_content
    }
    return _dg_finish(_pd_apply(event, _qp, deal_id, response))   
