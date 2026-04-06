import os
import re
import requests
from collections import deque
from urllib.parse import urljoin, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MAX_PAGES = 16
TIMEOUT = 15

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

STATE_TIMEZONE = {
    'CT': 'EST', 'DE': 'EST', 'FL': 'EST', 'GA': 'EST', 'IN': 'EST', 'ME': 'EST', 'MD': 'EST',
    'MA': 'EST', 'MI': 'EST', 'NH': 'EST', 'NJ': 'EST', 'NY': 'EST', 'NC': 'EST', 'OH': 'EST',
    'PA': 'EST', 'RI': 'EST', 'SC': 'EST', 'VT': 'EST', 'VA': 'EST', 'WV': 'EST', 'DC': 'EST',
    'AL': 'CST', 'AR': 'CST', 'IL': 'CST', 'IA': 'CST', 'KS': 'CST', 'KY': 'CST', 'LA': 'CST',
    'MN': 'CST', 'MS': 'CST', 'MO': 'CST', 'NE': 'CST', 'ND': 'CST', 'OK': 'CST', 'SD': 'CST',
    'TN': 'CST', 'TX': 'CST', 'WI': 'CST',
    'AZ': 'MST', 'CO': 'MST', 'ID': 'MST', 'MT': 'MST', 'NM': 'MST', 'UT': 'MST', 'WY': 'MST', 'NV': 'MST',
    'AK': 'AKST', 'CA': 'PST', 'OR': 'PST', 'WA': 'PST', 'HI': 'HST'
}

PRIORITY_PATHS = [
    '/contact', '/contact-us', '/contacts', '/pages/contact-us', '/pages/contact',
    '/about', '/about-us', '/pages/about', '/pages/about-us', '/support', '/customer-service',
    '/help', '/faq', '/faqs', '/policies', '/privacy-policy', '/terms', '/legal'
]

B2B_PATHS = [
    '/wholesale', '/become-a-dealer', '/become-a-distributor', '/distributor', '/distributors',
    '/partner', '/partners', '/reseller', '/pages/wholesale', '/pages/become-a-dealer', '/b2b',
    '/trade', '/sell-with-us', '/open-an-account', '/dealer', '/dealers', '/business', '/retailers'
]

SKIP_PATH_KEYWORDS = [
    'find-a-dealer', 'find-dealer', 'store-locator', 'store_locator', 'dealer-locator', 'where-to-buy',
    'where_to_buy', 'locations', 'distributors-list', 'dealer-list', 'shop-locator', 'find-a-store',
    'retail-locations', 'sales-reps', 'rep-locator'
]

SOCIAL_SKIP = ['sharer', 'share?', 'dialog', 'plugins', 'intent', 'sharearticle', 'feed/update', 'authwall', 'login']
ADDRESS_WORDS = ['street', 'st.', 'st ', 'road', 'rd', 'ave', 'avenue', 'drive', 'dr', 'blvd', 'suite', 'ste', 'lane', 'ln', 'court', 'ct']

PHONE_RE = re.compile(r'(?<!\d)(?:\+?1[\s\-.]?)?\(?([2-9]\d{2})\)?[\s\-.]([2-9]\d{2})[\s\-.](\d{4})(?!\d)')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
ADDRESS_INLINE_RE = re.compile(r'(?<!\d)(\d{1,6}\s+[A-Za-z0-9#.,\- ]{4,90}?)\s+([A-Za-z .\-]{2,40}),?\s+([A-Z]{2})\s*(\d{5}(?:-\d{4})?)(?!\d)')
ADDRESS_CITYLESS_RE = re.compile(r'(?<!\d)(\d{1,6}\s+[A-Za-z0-9#.,\- ]{4,90}?)\s+([A-Z]{2})\s*(\d{5}(?:-\d{4})?)(?!\d)')
RAW_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)


def safe_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def get_domain(url):
    try:
        return urlparse(url).netloc.lower().replace('www.', '')
    except Exception:
        return ''


def normalize_url(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path or ''}".rstrip('/') or f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def infer_source_type(page_url, root_domain):
    lower = (page_url or '').lower()
    if 'facebook.com' in lower:
        return 'facebook'
    if 'linkedin.com' in lower:
        return 'linkedin'
    return 'website' if get_domain(page_url) == root_domain else 'fallback'


def fetch_html(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ct = resp.headers.get('content-type', '').lower()
        if resp.ok and ('text/html' in ct or 'application/xhtml+xml' in ct):
            return resp.text, str(resp.url)
    except Exception:
        pass
    return None, None


def is_bogus_email(email):
    email = (email or '').lower()
    bad = ['example', 'noreply', 'no-reply', 'test@', 'demo@', 'placeholder', 'yourname', 'name@']
    return any(b in email for b in bad)


def normalize_contact_item(item):
    if isinstance(item, str):
        return {'value': item.strip(), 'source_type': '', 'source_url': '', 'label': ''}
    if isinstance(item, dict):
        return {
            'value': safe_text(item.get('value', '')),
            'source_type': safe_text(item.get('source_type', '')),
            'source_url': safe_text(item.get('source_url', '')),
            'label': safe_text(item.get('label', '')),
        }
    return {'value': '', 'source_type': '', 'source_url': '', 'label': ''}


def dedupe_contact_items(items):
    out = []
    seen = set()
    for raw in items:
        item = normalize_contact_item(raw)
        if not item['value']:
            continue
        key = item['value'].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def same_digits(a, b):
    return re.sub(r'\D', '', str(a or '')) == re.sub(r'\D', '', str(b or ''))


def classify_linkedin(url):
    lower = url.lower()
    if 'linkedin.com' not in lower:
        return False
    if any(x in lower for x in SOCIAL_SKIP):
        return False
    return True


def extract_social_candidates(soup, html):
    candidates = []
    for el in soup.find_all(True):
        for attr in ('href', 'data-href', 'onclick', 'data-url', 'data-link', 'data-social'):
            raw = el.get(attr)
            if raw:
                candidates.append(str(raw))
    for raw in RAW_URL_RE.findall(html or ''):
        candidates.append(raw)
    cleaned = []
    for raw in candidates:
        if 'window.open' in raw:
            cleaned.extend(RAW_URL_RE.findall(raw))
        else:
            cleaned.append(raw)
    return cleaned


def extract_socials(soup, html, page_url, root_domain):
    facebook = None
    linkedin = None
    for raw in extract_social_candidates(soup, html):
        lower = raw.lower()
        if not facebook and 'facebook.com' in lower and not any(x in lower for x in SOCIAL_SKIP):
            facebook = {'value': raw.strip(), 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url}
        if not linkedin and classify_linkedin(lower):
            linkedin = {'value': raw.strip(), 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url}
    return facebook, linkedin


def candidate_blocks(soup):
    selectors = ['address', 'footer', 'header', 'section', 'article', 'div', 'p', 'li', 'td', 'span']
    blocks = []
    seen = set()
    for tag in soup.find_all(selectors):
        text = safe_text(tag.get_text(' ', strip=True))
        if len(text) < 8:
            continue
        if text in seen:
            continue
        seen.add(text)
        blocks.append((tag, text))
    return blocks


def make_phone(num):
    digits = re.sub(r'\D', '', num)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return ''
    if digits in ('5555555555', '1234567890') or '555' in digits[:6]:
        return ''
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def extract_contact_blocks(soup, page_url, root_domain):
    phones = []
    faxes = []
    emails = []

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        label = safe_text(a.get_text(' ', strip=True))
        if href.lower().startswith('mailto:'):
            email = href[7:].split('?')[0].strip().lower()
            if '@' in email and not is_bogus_email(email):
                emails.append({'value': email, 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url, 'label': label})
        if href.lower().startswith('tel:'):
            normalized = make_phone(href[4:])
            if normalized:
                phones.append({'value': normalized, 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url, 'label': label})

    for tag, text in candidate_blocks(soup):
        lower = text.lower()
        found_emails = {m.group(0).lower() for m in EMAIL_RE.finditer(text)}
        for email in found_emails:
            if not is_bogus_email(email):
                emails.append({'value': email, 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url, 'label': block_label(lower)})
        for m in PHONE_RE.finditer(text):
            normalized = make_phone(''.join(m.groups()))
            if not normalized:
                continue
            context_before = lower[max(0, m.start() - 80):m.start()]
            context_after = lower[m.end():m.end() + 80]
            label = block_label(lower)
            item = {'value': normalized, 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url, 'label': label}
            if any(x in context_before or x in context_after or x in lower for x in ['fax', 'facsimile', 'f:']):
                faxes.append(item)
            else:
                phones.append(item)

    phones = dedupe_contact_items(phones)
    faxes = dedupe_contact_items(faxes)
    phones = [p for p in phones if not any(same_digits(p['value'], f['value']) for f in faxes)]
    same_domain = [e for e in dedupe_contact_items(emails) if e['value'].split('@')[-1] == root_domain]
    emails = same_domain if same_domain else dedupe_contact_items(emails)
    return phones, faxes, emails


def block_label(lower_text):
    pairs = [
        ('fax', 'Fax'), ('customer service', 'Customer Service'), ('support', 'Support'),
        ('sales', 'Sales'), ('corporate', 'Corporate'), ('office', 'Office'), ('contact', 'Contact')
    ]
    for needle, label in pairs:
        if needle in lower_text:
            return label
    return ''


def score_contact_page(url, title, text):
    score = 0
    lower_url = (url or '').lower()
    lower_text = (text or '').lower()
    lower_title = (title or '').lower()
    for needle, pts in [('contact', 6), ('support', 5), ('customer service', 5), ('about', 2), ('help', 3), ('faq', 1)]:
        if needle in lower_url:
            score += pts
        if needle in lower_title:
            score += pts
        if needle in lower_text[:1200]:
            score += max(1, pts - 1)
    return score


def score_b2b_page(url, title, text):
    score = 0
    lower_url = (url or '').lower()
    lower_text = (text or '').lower()
    lower_title = (title or '').lower()
    needles = [
        ('wholesale', 7), ('dealer', 6), ('distributor', 6), ('reseller', 6), ('b2b', 6),
        ('trade', 4), ('business account', 6), ('open an account', 6), ('become a dealer', 7),
        ('become a distributor', 7), ('retailer', 4), ('partner', 3)
    ]
    for needle, pts in needles:
        if needle in lower_url:
            score += pts
        if needle in lower_title:
            score += pts
        if needle in lower_text[:2500]:
            score += max(1, pts - 1)
    return score


def extract_address_from_blocks(soup, page_url, root_domain):
    best = None
    for tag, text in candidate_blocks(soup):
        lower = text.lower()
        if len(text) > 220:
            continue
        if not any(w in lower for w in ADDRESS_WORDS) and not tag.name == 'address':
            continue
        m = ADDRESS_INLINE_RE.search(text)
        if m:
            line1 = safe_text(m.group(1))
            city = safe_text(m.group(2))
            state = safe_text(m.group(3)).upper()
            zip_code = safe_text(m.group(4))
            if state in STATE_TIMEZONE:
                line = f"{line1}, {city}, {state} {zip_code}"
                return {'line': line, 'state': state, 'zip': zip_code, 'timezone': STATE_TIMEZONE[state], 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url}
        m2 = ADDRESS_CITYLESS_RE.search(text)
        if m2:
            line1 = safe_text(m2.group(1))
            state = safe_text(m2.group(2)).upper()
            zip_code = safe_text(m2.group(3))
            if state in STATE_TIMEZONE:
                best = {'line': f"{line1}, {state} {zip_code}", 'state': state, 'zip': zip_code, 'timezone': STATE_TIMEZONE[state], 'source_type': infer_source_type(page_url, root_domain), 'source_url': page_url}
    return best


def discover_internal_links(soup, current_url, root_domain):
    links = []
    for a in soup.find_all('a', href=True):
        abs_url = urljoin(current_url, a['href'])
        parsed = urlparse(abs_url)
        if parsed.scheme not in ('http', 'https'):
            continue
        domain = parsed.netloc.lower().replace('www.', '')
        if domain != root_domain:
            continue
        lower = abs_url.lower()
        if any(k in lower for k in SKIP_PATH_KEYWORDS):
            continue
        anchor = safe_text(a.get_text(' ', strip=True)).lower()
        weight = 0
        for kw in ['contact', 'about', 'support', 'faq', 'wholesale', 'dealer', 'distributor', 'reseller', 'b2b', 'trade', 'partner', 'open an account', 'retailer']:
            if kw in lower or kw in anchor:
                weight += 3
        if weight > 0:
            links.append((normalize_url(abs_url), weight))
    links.sort(key=lambda x: x[1], reverse=True)
    return [u for u, _ in links]


def gather_company_snippets(soup):
    snippets = []
    title = safe_text(soup.title.get_text(' ', strip=True)) if soup.title else ''
    if title:
        snippets.append(title)
    for el in soup.find_all(['meta']):
        name = (el.get('name') or el.get('property') or '').lower()
        if name in ('og:site_name', 'application-name', 'twitter:title', 'og:title'):
            content = safe_text(el.get('content', ''))
            if content:
                snippets.append(content)
    text = safe_text(soup.get_text(' ', strip=True))
    for marker in ['copyright', 'all rights reserved', 'llc', 'inc', 'corp', 'corporation', 'company']:
        idx = text.lower().find(marker)
        if idx >= 0:
            snippets.append(text[max(0, idx - 80): idx + 120])
    return dedupe_strings(snippets)[:10]


def dedupe_strings(values):
    out = []
    seen = set()
    for raw in values:
        value = safe_text(raw)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def crawl_site(root_url):
    root = normalize_url(root_url)
    root_domain = get_domain(root)
    queue = deque()
    visited = set()
    pages = []

    for path in PRIORITY_PATHS + B2B_PATHS:
        queue.append(normalize_url(origin(root) + path))
    queue.append(root)

    while queue and len(visited) < MAX_PAGES:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        html, final_url = fetch_html(url)
        if not html:
            continue
        final_url = normalize_url(final_url or url)
        if get_domain(final_url) != root_domain:
            continue

        soup = BeautifulSoup(html, 'lxml')
        title = safe_text(soup.title.get_text(' ', strip=True)) if soup.title else ''
        phones, faxes, emails = extract_contact_blocks(soup, final_url, root_domain)
        facebook, linkedin = extract_socials(soup, html, final_url, root_domain)
        address = extract_address_from_blocks(soup, final_url, root_domain)
        text = safe_text(soup.get_text(' ', strip=True))
        page = {
            'url': final_url,
            'title': title,
            'phones': phones,
            'faxes': faxes,
            'emails': emails,
            'facebook': facebook,
            'linkedin': linkedin,
            'address': address,
            'contact_score': score_contact_page(final_url, title, text),
            'b2b_score': score_b2b_page(final_url, title, text),
            'company_snippets': gather_company_snippets(soup),
        }
        pages.append(page)

        for internal in discover_internal_links(soup, final_url, root_domain):
            if internal not in visited and internal not in queue and len(queue) < MAX_PAGES * 3:
                queue.append(internal)

    all_emails = dedupe_contact_items([item for page in pages for item in page['emails']])
    same_domain_emails = [e for e in all_emails if e['value'].split('@')[-1] == root_domain]
    all_faxes = dedupe_contact_items([item for page in pages for item in page['faxes']])
    all_phones = [p for p in dedupe_contact_items([item for page in pages for item in page['phones']]) if not any(same_digits(p['value'], f['value']) for f in all_faxes)]

    facebook = next((page['facebook'] for page in pages if page['facebook']), {'value': '', 'source_type': '', 'source_url': ''})
    linkedin = next((page['linkedin'] for page in pages if page['linkedin']), {'value': '', 'source_type': '', 'source_url': ''})
    address = next((page['address'] for page in pages if page['address']), None)
    contact_page = best_page_link(pages, 'contact_score')
    b2b_page = best_page_link(pages, 'b2b_score')

    return {
        'phones': all_phones,
        'faxes': all_faxes,
        'emails': same_domain_emails if same_domain_emails else all_emails,
        'facebook': facebook,
        'linkedin': linkedin,
        'contact_page': contact_page,
        'b2b_page': b2b_page,
        'address': address,
        'timezone': address.get('timezone', '') if address else '',
        'page_titles': dedupe_strings([page['title'] for page in pages])[:12],
        'company_snippets': dedupe_strings([snippet for page in pages for snippet in page['company_snippets']])[:12],
    }


def best_page_link(pages, score_key):
    ranked = [p for p in pages if p.get(score_key, 0) > 0]
    if not ranked:
        return {'value': '', 'source_type': '', 'source_url': ''}
    ranked.sort(key=lambda p: p.get(score_key, 0), reverse=True)
    best = ranked[0]
    return {'value': best['url'], 'source_type': 'website', 'source_url': best['url']}


@app.route('/research', methods=['POST'])
def research():
    data = request.get_json(force=True)
    website_url = safe_text(data.get('website_url', ''))
    if not website_url:
        return jsonify({'ok': False, 'error': 'website_url is required'}), 400
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
