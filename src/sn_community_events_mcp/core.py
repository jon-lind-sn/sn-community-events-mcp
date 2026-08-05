"""
Core fetch/parse logic for ServiceNow Community events
(https://www.servicenow.com/community/events/ct-p/TopLevel_Events).

No login is required -- the underlying API is public, verified with plain anonymous
curl (no cookies/session). See fetch_events() docstring for the pagination gotchas;
they cost real debugging time to work out and are easy to reintroduce by accident.

Stdlib only -- no third-party dependencies (curl is shelled out to; see curl_get()
for why urllib isn't used).
"""
import csv
import html
import json
import re
import subprocess
import urllib.parse
from datetime import date

BASE = "https://www.servicenow.com/community/s/plugins/custom/servicenow/servicenow"
PAGE_SIZE = 100

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
    )
}
for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
):
    MONTHS[m] = i + 1

# Matches "December 10, 2026", "Oct 20-22, 2026", "Oct 20, 2026" etc.
# Only the first day of a range is used as the sortable/filterable date.
DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*[-–]\s*(?:[A-Za-z]{3,9}\.?\s*)?\d{1,2})?,?\s+(\d{4})"
)

CARD_FIELD_RES = {
    "category": re.compile(r'slot="eyebrow">(.*?)</arc-text>', re.S),
    "title": re.compile(r'slot="heading">(.*?)</arc-heading>', re.S),
    "description": re.compile(r'slot="description">(.*?)</arc-text>', re.S),
    "date_text": re.compile(r'slot="date">(.*?)</arc-text>', re.S),
    "location": re.compile(r'slot="location">(.*?)</arc-text>', re.S),
    "url": re.compile(r'href="([^"]+)"'),
}

LINK_RE = re.compile(r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def strip_tags(s):
    """Strip HTML tags, decode entities, and turn any literal line break in the
    source text into a `<br>` marker (keeps CSV/text output one row per line while
    still preserving where the original line breaks were). `<a href>` links are kept
    as clean anchor tags rather than stripped -- they're often genuinely useful
    ("Register now" / event-page URLs). Everything else (spans, images, bold, etc.)
    is discarded."""
    if not s:
        return ""

    links = []

    def stash(m):
        href = html.escape(m.group(1), quote=True)
        text = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        text = re.sub(r"\s+", " ", text).strip()
        links.append(f'<a href="{href}">{text}</a>')
        return f"\x00{len(links) - 1}\x00"

    s = LINK_RE.sub(stash, s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\r\n|\r|\n", "<br>", s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = PLACEHOLDER_RE.sub(lambda m: links[int(m.group(1))], s)
    return s


def curl_get(url, timeout=25):
    """Fetch a URL with curl as raw text. Plain urllib has been observed to fail the
    TLS handshake against this Akamai-fronted host in some sandboxed environments
    (Claude Code's sandbox, notably); curl doesn't, so it's used unconditionally."""
    result = subprocess.run(
        ["curl", "-sL", "--max-time", str(timeout), url],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def curl_get_json(url, timeout=25):
    return json.loads(curl_get(url, timeout=timeout))


# Only the community's own event-post pages ("ev-p/<id>") carry the full,
# server-rendered description. External registration links (Zoom, info.servicenow.com,
# rsvp.servicenow.com, etc.) don't -- those keep whatever short blurb the listing gave us.
EVENT_PAGE_RE = re.compile(r"^https://www\.servicenow\.com/community/.*/ev-p/\d+/?$")
DESCRIPTION_BLOCK_RE = re.compile(
    r'<div class="lia-occasion-description">(.*?)<hr class="lia-content-divider lia-component-description"',
    re.S,
)
PARAGRAPH_RE = re.compile(r"<P[^>]*>(.*?)</P>", re.S | re.I)


def fetch_full_description(url):
    """Best-effort fetch of an event's full description from its own detail page.
    Returns None (caller should keep the listing's short blurb) if the page isn't a
    community event-post page, or if the expected markup isn't found -- ServiceNow's
    community template does vary slightly between event types."""
    if not EVENT_PAGE_RE.match(url):
        return None
    try:
        page = curl_get(url)
    except subprocess.CalledProcessError:
        return None
    block = DESCRIPTION_BLOCK_RE.search(page)
    if not block:
        return None
    paragraphs = []
    for p in PARAGRAPH_RE.findall(block.group(1)):
        text = strip_tags(p)
        if text and text != "\xa0":
            paragraphs.append(text)
    return "<br>".join(paragraphs) if paragraphs else None


def get_token():
    tid = "-" + str(abs(hash("sn-community-events")) % (10**16))
    return curl_get_json(f"{BASE}/get-sn-token?tid={tid}")["accessToken"]


def fetch_page(token, filters, cursor_k=None, cursor_b=None):
    params = {
        "pageSize": PAGE_SIZE,
        "moduleName": "community_events",
        "locale": "en-us",
        "accessToken": token,
    }
    params.update(filters)
    if cursor_k:
        params["cursor-k"] = cursor_k
        params["cursor-b"] = cursor_b
    url = f"{BASE}/post.db-events?" + urllib.parse.urlencode(params)
    return curl_get_json(url)


def parse_date(text):
    """Best-effort parse of the free-text date field. Returns a date or None."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    month_name, day, year = m.group(1).lower().rstrip("."), int(m.group(2)), int(m.group(3))
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_card(card_html):
    fields = {}
    for key, rx in CARD_FIELD_RES.items():
        m = rx.search(card_html)
        fields[key] = strip_tags(m.group(1)) if m else ""
    fields["parsed_date"] = parse_date(fields["date_text"])
    return fields


def fetch_events(start, end, filters=None, full_description=True, max_pages=500):
    """Fetch community events between `start` and `end` (inclusive `date` objects).

    filters: dict of optional {status-category, location-category, product-category,
    type-category} slug values, passed straight through to the API as query params.

    Returns (events, meta) where events is a list of dicts with keys:
    category, title, description, date_text, parsed_date (a `date`), location, url.
    meta is a dict of counters useful for reporting back to the caller (hops fetched,
    unparsed dates skipped, full descriptions fetched/unavailable).

    Gotchas worth knowing before changing this function:
    - The page's own "Showing 9,XXX results" counter and this API's total-pages/
      total-results fields come from an unrelated search index -- don't trust them.
    - The API's `page` query parameter is silently ignored; every request with the
      same pageSize and no cursor returns the identical top-N rows. Real pagination
      is a keyset cursor: feed cursor-k/cursor-b from a response back into the next
      request under those same field names to advance; omit both to restart at the top.
    - Results are sorted by event date descending (soonest-future first, walking
      backward through time as you page) -- this is what makes the early-exit below
      correct: once every date on a hop is older than `start`, nothing further out
      will ever be newer.
    - Some filtered queries return a non-empty cursor-k with an EMPTY cursor-b once
      the filtered result set is exhausted; resubmitting that pair 400s rather than
      cleanly signaling "no more data" -- treat an empty cursor-b as end-of-results.
    - The same event occasionally gets cross-posted to multiple community groups and
      shows up as a duplicate card with an identical detail URL -- dedupe on url.
    """
    filters = filters or {}
    token = get_token()
    matches = []
    seen_urls = set()
    skipped_unparsed = 0
    cursor_k, cursor_b = None, None
    hop = 0

    while True:
        data = fetch_page(token, filters, cursor_k, cursor_b)
        if "error" in data:
            raise RuntimeError(f"API error on hop {hop}: {data.get('msg', data['error'])}")

        results = data.get("results", [])
        if not results:
            break

        hop_dates = []
        for item in results:
            fields = parse_card(item.get("card", ""))
            d = fields["parsed_date"]
            if d is None:
                skipped_unparsed += 1
                continue
            hop_dates.append(d)
            if fields["url"] in seen_urls:
                continue
            if start <= d <= end:
                matches.append(fields)
                seen_urls.add(fields["url"])

        if hop_dates and max(hop_dates) < start:
            break

        next_k, next_b = data.get("cursor-k"), data.get("cursor-b")
        if not next_k or not next_b:
            break
        cursor_k, cursor_b = next_k, next_b

        hop += 1
        if hop > max_pages:
            break

    matches.sort(key=lambda f: f["parsed_date"])

    full_desc_fetched = 0
    full_desc_unavailable = 0
    if full_description:
        for m in matches:
            full = fetch_full_description(m["url"])
            if full:
                m["description"] = full
                full_desc_fetched += 1
            else:
                full_desc_unavailable += 1

    meta = {
        "count": len(matches),
        "hops_fetched": hop + 1,
        "skipped_unparsed_dates": skipped_unparsed,
        "full_descriptions_fetched": full_desc_fetched,
        "full_descriptions_unavailable": full_desc_unavailable,
    }
    return matches, meta


def write_csv(events, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["category", "title", "description", "date_text", "parsed_date", "location", "url"],
        )
        writer.writeheader()
        for e in events:
            row = dict(e)
            row["parsed_date"] = e["parsed_date"].isoformat()
            writer.writerow(row)
