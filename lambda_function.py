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
    {"id": "accept_bid",   "q": "Would you accept a bid of ___?",                                   "field": None},
    {"id": "deadline",     "q": "When is the deadline to commit?",                                  "field": None},
    {"id": "class",        "q": "Are these shares common or preferred?",                            "field": "Class"},
    {"id": "min_max",      "q": "What is the minimum / maximum size?",                              "field": "min_max"},
    {"id": "shares_avail", "q": "How many shares are available to buy?",                            "field": "Shares"},
    {"id": "seller_fee",   "q": "What is the seller's one-time fee?",                               "field": "Seller Fee"},
    {"id": "upfront_fee",  "q": "Would you accept an upfront fee instead of management fee and carry?", "field": None},
    {"id": "nda_l1",       "q": "Can you provide full transparency on the L1 manager under an NDA?", "field": None},
    {"id": "direct_trade", "q": "Do you have company permission to directly transfer?",              "field": None},
]
QUESTION_CATALOG_SELLER = [  # shown on BUY orders (a seller asking about the buyer)
    {"id": "accept_bid",   "q": "Would you accept a bid of ___?",               "field": None},
    {"id": "cash_on_hand", "q": "Do you have cash on hand?",                    "field": None},
    {"id": "qp_accredited","q": "Are you a QP or accredited?",                  "field": None},
    {"id": "iqf_done",     "q": "Have you completed the IQF with Rainmaker?",   "field": None},
    {"id": "on_cap_table", "q": "Are you already on the cap table?",            "field": None},
    {"id": "no_data_room", "q": "Is there a data room or financials available?", "field": None},
    {"id": "accept_common","q": "Would you accept common shares?",             "field": None},
    {"id": "move_bid_up",  "q": "Would you move your bid up?",                  "field": None},
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

def render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room=True):
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

    layers = map_option_value('Layers', mapped_fields.get('Layers', []))
    if isinstance(layers, (list, tuple)):
        layers = ', '.join(str(x) for x in layers)
    elif not isinstance(layers, str):
        layers = str(layers or '')
    is_multilayer = ('2-Layer' in layers) or ('3-Layer' in layers)
    is_tender = str(mapped_fields.get('Price Status', '')) == '7000239'

    catalog = [it for it in catalog
               if not (is_spv and it["id"] == "direct_trade")
               and not (it["id"] == "nda_l1" and not is_multilayer)]

    FORM_URL = "https://s5qv2qkmjt2qejliwchvqukseq0wgwff.lambda-url.us-east-1.on.aws/"

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
        if qid == "seller_fee" and (mapped_fields.get('Seller Fee') or not is_spv):
            continue
        if qid == "upfront_fee" and not is_spv:
            continue
        if qid == "accept_bid" and is_tender:
            continue
        if qid == "move_bid_up" and is_tender:
            continue
        if qid == "qp_accredited" and not is_spv:
            continue
        if qid == "no_data_room" and not ask_data_room:
            continue

        rows += (
            f'<label class="qa-row">'
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

    if not rows:
        return ''

    return (
        '<aside class="qa-box">'
        f'<h2>Send Question to {"Buyer" if deal_type == "Buy Order" else "Seller"}</h2>'
        f'<form method="POST" action="{FORM_URL}">'
        '<input type="hidden" name="qa" value="submit">'
        f'<input type="hidden" name="deal_id" value="{deal_id}">'
        + rows
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
        data_room_display = ''  # blank
    ask_data_room = (data_room_display == '')

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
    
    bid_button_text = "Offer" if map_option_value('Type', mapped_fields.get('Type', [])) == "Buy Order" else "Bid"

    deal_type = map_option_value('Type', mapped_fields.get('Type', []))
    qa_box_html = render_qa_box(deal_type, mapped_fields, deal_id, deal_name, ask_data_room)
    _msg_raw = (deal_data.get('custom_fields') or {}).get('custom_label_4001285')
    hide_questions = (str(_msg_raw) == '7187011')

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
            .deal-body {{ display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap; }}
            .deal-main {{ flex:1; min-width:320px; }}
            .qa-box {{ width:300px; border:1px solid var(--border-strong); border-radius:8px;
                       padding:14px 16px; background:#faf8f3; font-size:13px; }}
            .qa-box h2 {{ margin:0 0 10px 0; font-size:15px; }}
            .qa-row {{ display:flex; align-items:flex-start; gap:8px; padding:7px 0; border-bottom:1px solid var(--border-strong); cursor:pointer; }}
            .qa-row input[type=checkbox] {{ margin-top:3px; flex:none; }}
            .qa-bid {{ margin:2px 0 6px 26px; display:flex; gap:6px; }}
            .qa-bid input {{ width:50%; padding:5px; font-size:12px; box-sizing:border-box; }}
            .qa-email {{ width:100%; padding:7px; margin:12px 0 8px; box-sizing:border-box; font-size:13px; }}
            .qa-send {{ width:100%; padding:9px; font-size:13px; font-weight:600; cursor:pointer; border:none; border-radius:6px; background:var(--accent); color:#fff; }}
            .qa-send:hover {{ opacity:0.9; }}
            .qa-row:last-child {{ border-bottom:none; }}
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
        </style>
    </head>
    <body>
        <div class="header">
            <div class="header-content">
                <h1><strong>{deal_name}{get_structure_description(map_option_value('Structure', mapped_fields.get('Structure', [])))}</strong></h1>
                <div class="deal-id">Deal ID: {deal_id} <span class="copy-id" onclick="copyDealId('{deal_id}', this)" title="Copy Deal ID"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></span> (Updated: {format_date(deal_data.get('updated_at', ''))})</div>
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
                <a href="mailto:cgracia@rainmakersecurities.com?subject={urllib.parse.quote(f'{bid_button_text} on: {deal_name} - {deal_id}')}&body={urllib.parse.quote(f'Hello Chad,\n\nI would like to make this {bid_button_text.lower()} on {deal_name} (ID: {deal_id}).\n\n{bid_button_text}: $____________\nTransaction Size: ____________\n\nPlease let me know the next steps.\n\nThank you.')}" class="btn bid-btn">{bid_button_text}</a>
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
            {'' if hide_questions else qa_box_html}
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
