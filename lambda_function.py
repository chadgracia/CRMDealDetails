import json
import urllib.request
import urllib.error
import urllib.parse
import logging
from datetime import datetime, timedelta  # NEW: Import for date handling

API_KEY = "defe67185ffd4993874558be8c4eb29b"  # Your actual NewsAPI key

# Companies whose news search returns mostly junk -- the News section is skipped
# for these. To turn news off for a company, add its name here (case-insensitive,
# matched against the company name exactly). To turn it back on, remove the line.
NEWS_SKIP_COMPANIES = {
    "1x",
}

QUESTION_CATALOG_BUYER = [   # shown on SELL orders (a buyer asking about the seller)
    {"id": "accept_bid",   "q": "Would you accept a bid of $___/share (gross)?",                    "field": None},
    {"id": "deadline",     "q": "When is the deadline to commit?",                                  "field": None},
    {"id": "class",        "q": "Are these shares common or preferred?",                            "field": "Class"},
    {"id": "min_max",      "q": "What is the minimum / maximum size?",                              "field": "min_max"},
    {"id": "shares_avail", "q": "How many shares are available to buy?",                            "field": "Shares"},
    {"id": "seller_fee",   "q": "What is the seller's one-time fee?",                               "field": "Seller Fee"},
    {"id": "fee_structure","q": "Would you accept this fee structure?",                              "field": None},
    {"id": "data_room_avail","q": "Is a data room available for diligence?",                         "field": None},
    {"id": "fund_exemption", "q": "Is this a 3(c)(1) or 3(c)(7) exemption?",                         "field": None},
    {"id": "nda_l1",       "q": "Can you provide full transparency on the L1 manager under an NDA?", "field": None},
    {"id": "direct_trade", "q": "Do you have company permission to directly transfer?",              "field": None},
]
QUESTION_CATALOG_SELLER = [  # shown on BUY orders (a seller asking about the buyer)
    {"id": "accept_bid",   "q": "Would you bid $___/share (gross)?",            "field": None},
    {"id": "deadline",     "q": "When is the deadline to commit?",              "field": None},
    {"id": "cash_on_hand", "q": "Do you have cash on hand?",                    "field": None},
    {"id": "qp_accredited","q": "Are you a QP or accredited?",                  "field": None},
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
        return json.loads(obj['Body'].read().decode('utf-8'))
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
        'custom_label_3938753': 'GP Audit Status',
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
        'custom_label_3938749': 'Ownership Status',
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
        'GP Audit Status': {
            '7000246': 'Yes, the GP is audited.',
            '7000247': 'No',
            '7000248': "Don't know."
        },
        'SPVs Managed': {
            '7000249': 'This would be the first.',
            '7000250': '2-3',
            '7000251': '4-5',
            '7000252': '6-10',
            '7000253': 'More than 10'
        },
        'Fund Exemption': {
            '7200027': '3(c)(1)',
            '7200028': '3(c)(7) - Qualified Purchasers only'
        },
        'Class': {
            '5077831': 'Common',
            '5077834': 'Preferred',
            '5077912': 'Mixed',
            '5077915': 'Any'
        },
        'Seller Type': {
            '7000231': 'A GP (SPV manager) who owns the units for sale.',
            '7000232': 'A GP (SPV manager) who is facilitating an LP sale of units.',
            '7000233': 'An LP of an SPV who owns the units, with permission from GP to sell.',
            '7000234': 'An LP of an SPV who owns the units; needs permission from GP to sell.',
            '7000235': 'An owner with shares held via an online platform such as Forge, EquityZen, etc.',
            '7020357': 'A GP (SPV manager) who is collecting firm orders as part of their bid.'
        },
        'Ownership Status': {
            '7000236': 'Seller has legal ownership of shares now.',
            '7000237': 'Seller intends to acquire shares.'
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

def render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room=True, owner_iqf_yes=False):
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

        # If the answer is already visible in the left-hand tables, omit the question.
        if qid == "class" and mapped_fields.get('Class'):
            continue
        if qid == "min_max" and mapped_fields.get('Min Deal Size') and mapped_fields.get('Max Deal Size'):
            continue
        if qid == "shares_avail" and mapped_fields.get('Shares'):
            continue
        # SPV with a known max ticket whose price tracks a tender/round: the max
        # share count is moot, so don't ask it.
        if qid == "shares_avail" and is_spv and is_tender and mapped_fields.get('Max Deal Size'):
            continue
        if qid == "seller_fee" and (mapped_fields.get('Seller Fee') or not is_spv):
            continue
        if qid == "fee_structure" and not is_spv:
            continue
        if qid == "accept_bid" and is_tender:
            continue
        if qid == "accept_bid" and deal_type == "Sell Order" and str(mapped_fields.get('Ownership Status', '')) == '7000237':
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
                '<input type="number" name="bid_amount" step="any" placeholder="Bid $/share">'
                '<input type="number" name="bid_size" step="any" placeholder="Size $ (opt)">'
                '</div>'
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

def lambda_handler(event, context):
    logger.info("Lambda function started")
    logger.info(f"Incoming event: {json.dumps(event)}")

    deal_id = event.get('queryStringParameters', {}).get('deal_id')
    
    # If not found in query parameters, try path parameters
    if not deal_id and event.get('pathParameters'):
        deal_id = event.get('pathParameters', {}).get('deal_id')
        
    if not deal_id:
        return {'statusCode': 400, 'body': json.dumps({"error": "deal_id is required"})}
    
    logger.info(f"Extracted deal_id: {deal_id}")

    deal_data = fetch_deal_data(deal_id)
    
    if not deal_data:
        return {'statusCode': 404, 'body': json.dumps({"error": "Deal not found"})}
    
    custom_fields = deal_data.get('custom_fields', {})
    mapped_fields = map_custom_fields(custom_fields)
    
    deal_name = deal_data.get('name', 'Unknown Deal')
    
    # Extract company data
    company_data = deal_data.get('company_data', {})
    # Extract company name
    company_name = company_data.get('name', 'Unknown Company')

    # Fetch news data for the company - using exact phrase matching with business context
    business_context = "funding OR pre-IPO OR finance OR investment OR technology OR startup OR venture OR company OR business"
    news_html = ""

    # Skip the News section for companies whose search returns mostly junk (see NEWS_SKIP_COMPANIES)
    news_data = None
    if company_name.strip().lower() in NEWS_SKIP_COMPANIES:
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

    table_data = [
        ("Type", map_option_value('Type', mapped_fields.get('Type', []))),
        ("Class", map_option_value('Class', mapped_fields.get('Class', ''))),
        ("Net", net_with_valuation),
        ("Gross", gross_with_valuation),
        ("Shares", "{:,.0f}".format(float(mapped_fields.get('Shares', 0))) if mapped_fields.get('Shares') is not None else ''),
        ("Company LR (PPS)", format_currency(company_lr_pps, include_cents=True)),
        ("Company LR Val ($Bn)", format_currency(company_lr_val, include_cents=True)),
        ("Min Deal Size", format_currency(mapped_fields.get('Min Deal Size', ''))),
        ("Max Deal Size", format_currency(mapped_fields.get('Max Deal Size', ''))),
        ("Notes", deal_data.get('summary', ''))
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

    spv_data = [
        ("Layers", map_option_value('Layers', mapped_fields.get('Layers', ''))),
        ("Management Fee", format_percentage(mapped_fields.get('Management Fee', ''))),
        ("Carry", format_percentage(mapped_fields.get('Carry', ''))),
        ("Seller Fee", format_percentage(mapped_fields.get('Seller Fee', ''))),
        ("Partner Fee", format_percentage(mapped_fields.get('Partner Fee', ''))),
        ("Seller Type", map_option_value('Seller Type', mapped_fields.get('Seller Type', ''))),
        ("Price Status", map_option_value('Price Status', mapped_fields.get('Price Status', ''))),
        ("Ownership Status", map_option_value('Ownership Status', mapped_fields.get('Ownership Status', ''))),
        ("Data Room / VDR Available", data_room_display),
        ("SPV Jurisdiction", map_option_value('SPV Jurisdiction', mapped_fields.get('SPV Jurisdiction', ''))),
        ("GP Audit Status", map_option_value('GP Audit Status', mapped_fields.get('GP Audit Status', ''))),
        ("SPVs Managed", map_option_value('SPVs Managed', mapped_fields.get('SPVs Managed', '')))
    ]
    if mapped_fields.get('Fund Exemption'):
        spv_data.append(("Fund Exemption", map_option_value('Fund Exemption', mapped_fields.get('Fund Exemption', ''))))
    _final_deadline = (company_custom_fields.get('custom_label_3902620') or '')
    if _final_deadline:
        spv_data.insert(0, ("Deadline", str(_final_deadline)[:10]))
    
    bid_button_text = "Offer" if map_option_value('Type', mapped_fields.get('Type', [])) == "Buy Order" else "Bid"

    deal_type = map_option_value('Type', mapped_fields.get('Type', []))
    owner_iqf_yes = False
    if deal_type == "Buy Order":
        owner_iqf_yes = fetch_person_iqf_yes((deal_data.get('primary_contact') or {}).get('id'))
    qa_box_html = render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room, owner_iqf_yes)
    _msg_raw = (deal_data.get('custom_fields') or {}).get('custom_label_4001285')
    hide_questions = (str(_msg_raw) == '7187011')
    similar_html = render_similar_companies(company_name, deal_type, deal_id)
    _side_inner = ('' if hide_questions else qa_box_html) + similar_html
    side_col_html = '<div class="side-col">' + _side_inner + '</div>' if _side_inner.strip() else ''

    def generate_table_html(data):
        mid = len(data) // 2
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


    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{deal_name}</title>
        <link rel="stylesheet" href="https://s3.us-east-1.amazonaws.com/main.css/master.css">
        <style>
            /* Page-specific layout only. The shared Gracia look (font stack,
               page container, header, buttons, tables, news list, disclaimer)
               comes from master.css. */
            .header {{
                flex-wrap: wrap;
            }}
            .header-content {{
                flex: 1;
                min-width: 300px;
            }}
            .logo-container {{
                margin: 0 20px;
                display: flex;
                justify-content: center;
                align-items: center;
            }}
            .company-logo {{
                max-width: 120px;
                max-height: 80px;
                object-fit: contain;
            }}
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
            .similar-count {{ font-size:10px; font-weight:600; margin-left:4px;
                              color:inherit; opacity:0.65; }}
            .qa-row {{ display:flex; align-items:flex-start; gap:8px; padding:7px 0; border-bottom:1px solid var(--border-strong); cursor:pointer; }}
            .qa-row input[type=checkbox] {{ margin-top:3px; flex:none; }}
            .qa-bid {{ margin:2px 0 6px 26px; display:flex; gap:6px; }}
            .qa-bid input {{ width:50%; padding:5px; font-size:12px; box-sizing:border-box; }}
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
        </style>
    </head>
    <body>
        <div class="header">
            <div class="header-content">
                <h1><strong>{deal_name}{get_structure_description(map_option_value('Structure', mapped_fields.get('Structure', [])))}</strong></h1>
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
                <a href="{DESK_URL}/bid/?name={urllib.parse.quote(company_name)}&side={'sell' if bid_button_text == 'Offer' else 'buy'}&deal_id={deal_id}{f'&px={urllib.parse.quote(str(gross_price))}' if gross_price else ''}" class="btn bid-btn">{bid_button_text}</a>
                <a href="https://trades.graciagroup.com/" class="btn">Full Books</a>            
            </div>
        </div>
        <div class="company-summary">{company_summary}</div>
        {catalyst_html}

        <div class="deal-body">
            <div class="deal-main">
{generate_table_html(table_data)}

        <div id="spvSection" style="display: {'' if map_option_value('Structure', mapped_fields.get('Structure', [])) == 'Fund' else 'none'}">
        <h2>SPV Details</h2>
        {generate_table_html(spv_data)}
        </div>
            </div>
            {side_col_html}
        </div>
        <!-- News Section -->
        <div id="newsSection" class="news-section">
            {news_html}
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
    </body>

    </html>
    """
    
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/html'},
        'body': html_content
    }   
