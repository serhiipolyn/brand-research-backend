import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
CORS(app)

MAX_PAGES = 12
TIMEOUT = 10

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

STATE_TIMEZONE = {
    'CT':'EST','DE':'EST','FL':'EST','GA':'EST','IN':'EST','ME':'EST','MD':'EST',
    'MA':'EST','MI':'EST','NH':'EST','NJ':'EST','NY':'EST','NC':'EST','OH':'EST',
    'PA':'EST','RI':'EST','SC':'EST','VT':'EST','VA':'EST','WV':'EST','DC':'EST',
    'AL':'CST','AR':'CST','IL':'CST','IA':'CST','KS':'CST','KY':'CST','LA':'CST',
    'MN':'CST','MS':'CST','MO':'CST','NE':'CST','ND':'CST','OK':'CST','SD':'CST',
    'TN':'CST','TX':'CST','WI':'CST',
    'AZ':'MST','CO':'MST','ID':'MST','MT':'MST','NM':'MST','UT':'MST','WY':'MST','NV':'MST',
    'AK':'AKST','CA':'PST','OR':'PST','WA':'PST','HI':'HST'
}

PRIORITY_PATHS = [
    '/contact', '/contact-us', '/contacts', '/pages/contact-us', '/pages/contact',
    '/about', '/about-us', '/pages/about', '/pages/about-us', '/support',
]

B2B_PATHS = [
    '/wholesale', '/dealer', '/dealers', '/become-a-dealer', '/become-a-distributor',
    '/distributor', '/partner', '/partners', '/reseller', '/pages/wholesale',
    '/pages/dealer', '/pages/become-a-dealer', '/b2b', '/trade', '/sell-with-us',
]

SKIP_DOMAINS = {
    'amazon', 'ebay', 'walmart', 'target', 'homedepot', 'lowes', 'wayfair',
    'etsy', 'alibaba', 'aliexpress', 'shopify', 'google', 'facebook', 'instagram',
    'twitter', 'linkedin', 'youtube', 'pinterest', 'yelp', 'reddit', 'wikipedia',
    'zoominfo', 'crunchbase', 'bloomberg', 'thomasnet', 'grainger', 'globalspec',
    'dnb', 'hoovers', 'manta', 'bizapedia', 'opencorporates',
}


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_html(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.ok and 'text/html' in resp.headers.get('content-type', ''):
            return resp.text
    except Exception:
        pass
    return None


def get_domain(url):
    try:
        return urlparse(url).netloc.lower().replace('www.', '')
    except Exception:
        return ''


def normalize_url(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


# ── Contact extractors ────────────────────────────────────────────────────────

def extract_phones(soup, text):
    phones = set()
    faxes = set()

    # tel: links — most reliable
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('tel:'):
            digits = re.sub(r'\D', '', href[4:])
            if len(digits) == 10 and digits[0] in '23456789':
                phones.add(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
            elif len(digits) == 11 and digits[0] == '1' and digits[1] in '23456789':
                phones.add(f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}")

    # Text pattern — strict US only, check context before number for fax
    phone_re = re.compile(r'(?<!\d)(?:\+?1[\s\-.]?)?\(?([2-9]\d{2})\)?[\s\-.]([2-9]\d{2})[\s\-.](\d{4})(?!\d)')
    for m in phone_re.finditer(text):
        normalized = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
        digits = m.group(1) + m.group(2) + m.group(3)
        # Skip bogus numbers
        if digits in ('5555555555', '1234567890') or '555' in digits[:6]:
            continue
        before = text[max(0, m.start() - 60):m.start()].lower()
        if re.search(r'fax|facsimile', before):
            faxes.add(normalized)
        else:
            phones.add(normalized)

    # Remove phones that are also faxes
    phones -= faxes
    return sorted(phones), sorted(faxes)


def extract_emails(soup, root_domain):
    emails = set()
    # mailto: links
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('mailto:'):
            email = href[7:].split('?')[0].strip().lower()
            if '@' in email and not is_bogus_email(email):
                emails.add(email)
    # Text pattern
    text = soup.get_text(' ')
    for m in re.finditer(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text):
        e = m.group(0).lower()
        if not is_bogus_email(e):
            emails.add(e)
    # Prefer same-domain emails
    same = [e for e in emails if e.split('@')[1] == root_domain]
    return sorted(same) if same else sorted(emails)


def is_bogus_email(email):
    bad = ['example', 'noreply', 'no-reply', 'test@', 'demo@', 'placeholder', 'yourname', 'name@']
    return any(b in email for b in bad)


def extract_socials(soup):
    facebook = ''
    linkedin = ''
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if not facebook and 'facebook.com' in href:
            if not any(x in href for x in ['sharer', 'share?', 'dialog', 'plugins']):
                facebook = a['href']
        if not linkedin and 'linkedin.com/company' in href:
            linkedin = a['href']
    return facebook, linkedin


def extract_address(text):
    # Match: number street CITY STATE zip
    full_re = re.compile(r'\b(\d+\s+[A-Za-z0-9 .,\'#\-]{4,60}?)\s+([A-Z]{2})\.?\s*(\d{5})(?:-\d{4})?\b')
    for m in full_re.finditer(text):
        state = m.group(2).upper()
        if state in STATE_TIMEZONE:
            line = f"{m.group(1).strip()} {state} {m.group(3)}"
            return {'line': line, 'state': state, 'zip': m.group(3), 'timezone': STATE_TIMEZONE[state]}
    # Fallback: just state + zip
    fallback_re = re.compile(r'\b([A-Z]{2})\.?\s*(\d{5})(?:-\d{4})?\b')
    for m in fallback_re.finditer(text):
        state = m.group(1).upper()
        if state in STATE_TIMEZONE:
            return {'line': f"{state} {m.group(2)}", 'state': state, 'zip': m.group(2), 'timezone': STATE_TIMEZONE[state]}
    return None


# ── Site crawler ──────────────────────────────────────────────────────────────

def crawl_site(root_url):
    root_domain = get_domain(root_url)
    visited = set()
    results = {
        'phones': [], 'faxes': [], 'emails': [],
        'facebook': '', 'linkedin': '',
        'contact_page': '', 'b2b_page': '',
        'address': None,
    }

    all_phones = set()
    all_faxes = set()
    all_emails = set()

    # Build queue: priority contact/about pages first, then b2b, then root
    queue = []
    for path in PRIORITY_PATHS:
        queue.append(root_url.rstrip('/') + path)
    for path in B2B_PATHS:
        queue.append(root_url.rstrip('/') + path)
    queue.append(root_url)

    pages_crawled = 0

    for url in queue:
        if pages_crawled >= MAX_PAGES:
            break
        if url in visited:
            continue
        visited.add(url)

        html = fetch_html(url)
        if not html:
            continue

        pages_crawled += 1
        soup = BeautifulSoup(html, 'html.parser')

        # Remove script/style
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        text = soup.get_text('\n')

        # Phones & faxes
        p, f = extract_phones(soup, text)
        all_phones.update(p)
        all_faxes.update(f)

        # Emails
        emails = extract_emails(soup, root_domain)
        all_emails.update(emails)

        # Socials
        if not results['facebook'] or not results['linkedin']:
            fb, li = extract_socials(soup)
            if fb and not results['facebook']:
                results['facebook'] = fb
            if li and not results['linkedin']:
                results['linkedin'] = li

        # Address
        if not results['address']:
            results['address'] = extract_address(text)

        # Contact page
        url_lower = url.lower()
        if not results['contact_page'] and any(x in url_lower for x in ['/contact', '/support', '/about']):
            results['contact_page'] = url

        # B2B page
        if not results['b2b_page'] and any(x in url_lower for x in ['/wholesale', '/dealer', '/distributor', '/b2b', '/partner', '/reseller']):
            results['b2b_page'] = url

        # Crawl internal links from this page (add useful ones to queue)
        if pages_crawled < MAX_PAGES:
            for a in soup.find_all('a', href=True):
                abs_url = urljoin(url, a['href'])
                abs_domain = get_domain(abs_url)
                if abs_domain == root_domain and abs_url not in visited:
                    href_lower = a['href'].lower()
                    if any(kw in href_lower for kw in ['contact', 'about', 'wholesale', 'dealer', 'distributor', 'support', 'b2b', 'partner', 'reseller']):
                        if abs_url not in queue:
                            queue.append(abs_url)

    # Finalize
    all_phones -= all_faxes
    results['phones'] = sorted(all_phones)
    results['faxes'] = sorted(all_faxes)

    same_domain = [e for e in all_emails if e.split('@')[1] == root_domain]
    results['emails'] = sorted(same_domain) if same_domain else sorted(all_emails)

    return results


# ── API endpoint ──────────────────────────────────────────────────────────────

@app.route('/research', methods=['POST'])
def research():
    data = request.get_json(force=True)
    website_url = data.get('website_url', '').strip()

    if not website_url:
        return jsonify({'error': 'website_url is required'}), 400

    if not website_url.startswith('http'):
        website_url = 'https://' + website_url

    try:
        result = crawl_site(website_url)
        return jsonify({'ok': True, 'data': result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
