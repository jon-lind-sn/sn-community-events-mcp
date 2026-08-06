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
from concurrent.futures import ThreadPoolExecutor
from datetime import date

BASE = "https://www.servicenow.com/community/s/plugins/custom/servicenow/servicenow"
SOURCE_URL = "https://www.servicenow.com/community/events/ct-p/TopLevel_Events"
PAGE_SIZE = 100

# The real facet values, pulled from the filter widget config embedded in the events
# page HTML (view-source on SOURCE_URL, look for `filter-group-html`) -- they're
# exact-case Title strings with spaces, NOT the lowercase-hyphenated slugs one might
# guess (e.g. "In Person", "SNUGs"), which is why guesses like "in-person"/"snug" were
# silently going nowhere server-side. product-category isn't included here: its list
# is long and open-ended, so callers are expected to pass/slugify their own value.
LOCATION_CATEGORY_VALUES = ["In Person", "Virtual", "Hybrid"]
STATUS_CATEGORY_VALUES = ["ongoing", "upcoming", "past", "on-demand"]
TYPE_CATEGORY_VALUES = [
    "Webinar", "360 Exchange", "Office Hours", "Workshop", "Academy",
    "SNUGs", "Ask the Experts", "Developer Meetups",
]


def normalize_facet_value(value, canonical_values):
    """Best-effort map a caller-supplied filter value onto the API's real facet value
    (see the *_CATEGORY_VALUES lists above), matching case/hyphen/underscore-
    insensitively so a natural guess like "in-person" or "snugs" still resolves to the
    real "In Person" / "SNUGs". Falls back to the original value unchanged if nothing
    matches, rather than raising -- these filters are unvalidated pass-through, so an
    unmatched guess just fails the same way it always has."""
    norm = lambda s: s.lower().replace("-", " ").replace("_", " ").strip()
    target = norm(value)
    for canon in canonical_values:
        if norm(canon) == target:
            return canon
    return value

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


def dedup_key(fields):
    """Secondary dedupe key for cross-posted duplicates that get distinct detail
    URLs (e.g. the same event re-posted under a slightly different URL slug).
    Primary dedupe is still on exact url; this is a fallback on normalized
    title + date. Deliberately excludes location -- observed duplicate postings
    of the identical event sometimes carry slightly different location text (e.g.
    one adds a room number the other omits), so requiring an exact location match
    would let those duplicates slip back through."""
    return (fields["title"].strip().lower(), fields["parsed_date"])


def matches_type_category_text(fields, type_category):
    """Case-insensitive substring match of a type-category value against an event's
    own category/title text (e.g. "SNUGs" against category "Copenhagen SNUG"). Used
    as a client-side supplement -- see the "type-category facet" gotcha in
    fetch_events()'s docstring for why the server-side filter alone isn't enough.
    Also tries the singular form (stripping one trailing "s") since the real facet
    value is plural ("SNUGs") but community-group category text is singular ("...
    SNUG")."""
    phrase = type_category.replace("-", " ").replace("_", " ").strip().lower()
    if not phrase:
        return False
    candidates = {phrase}
    if phrase.endswith("s"):
        candidates.add(phrase[:-1])
    haystacks = (fields["category"].lower(), fields["title"].lower())
    return any(c in h for c in candidates for h in haystacks)


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
    # Best-effort only: the card HTML carries no explicit virtual/in-person/hybrid
    # signal (and the API's own location-category facet has been observed to be
    # unreliable, mirroring the type-category gotcha above). A URL in `location`
    # (a Zoom/registration link) reliably means virtual; real address text reliably
    # means in-person -- but an EMPTY location is genuinely ambiguous: plenty of
    # in-person meetups (confirmed empirically) omit the address just as often as
    # virtual ones omit a link. Guessing either way there would be confidently
    # wrong, so that case is "unknown" rather than defaulted to virtual.
    loc = fields["location"]
    if loc.startswith("http"):
        fields["location_type"] = "virtual"
    elif loc:
        fields["location_type"] = "in-person"
    else:
        fields["location_type"] = "unknown"
    return fields


def _fetch_matches(token, start, end, filters, max_pages):
    """Page through post.db-events for `filters`, returning (matches, hops, skipped_unparsed).
    No full-description fetching or type-category keyword supplementing here -- just the
    raw paginate-and-collect loop, shared by both the primary and supplemental fetches in
    fetch_events()."""
    matches = []
    seen_urls = set()
    seen_keys = set()
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
            key = dedup_key(fields)
            if fields["url"] in seen_urls or key in seen_keys:
                continue
            if start <= d <= end:
                matches.append(fields)
                seen_urls.add(fields["url"])
                seen_keys.add(key)

        if hop_dates and max(hop_dates) < start:
            break

        next_k, next_b = data.get("cursor-k"), data.get("cursor-b")
        if not next_k or not next_b:
            break
        cursor_k, cursor_b = next_k, next_b

        hop += 1
        if hop > max_pages:
            break

    return matches, hop, skipped_unparsed


def fetch_events(start, end, filters=None, full_description=True, max_pages=500):
    """Fetch community events between `start` and `end` (inclusive `date` objects).

    filters: dict of optional {status-category, location-category, product-category,
    type-category} slug values, passed straight through to the API as query params.

    Returns (events, meta) where events is a list of dicts with keys:
    category, title, description, date_text, parsed_date (a `date`), location, url.
    meta is a dict of counters useful for reporting back to the caller (hops fetched,
    unparsed dates skipped, full descriptions fetched/unavailable, type-category
    keyword-supplement matches added).

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
      shows up as a duplicate card -- usually with an identical detail URL, but
      sometimes with a distinct one (e.g. a "-1" suffix variant), so dedupe on url
      AND on a normalized title+date+location key (see dedup_key()).
    - The `type-category` facet (e.g. "snug", "webinar") is its OWN tag, independent
      of an event's `category` text (its community group name, e.g. "Federal and
      U.S. Public Sector SNUG"). It is NOT "every event posted under a SNUG group" --
      confirmed empirically: `type-category=snug` returns only a couple of events
      explicitly tagged that way, while dozens of other genuinely SNUG-group events
      in the same window carry some other type-category tag entirely. Relying on the
      server-side facet alone silently undercounts. To compensate, when a
      `type-category` filter is given we ALSO run an unfiltered (by type-category)
      fetch of the same window and keyword-match the slug against `category`/`title`
      text (matches_type_category_text()), merging in anything the facet missed.
    """
    filters = filters or {}
    token = get_token()

    matches, hop, skipped_unparsed = _fetch_matches(token, start, end, filters, max_pages)

    keyword_matches_added = 0
    type_category = filters.get("type-category")
    if type_category:
        seen_urls = {m["url"] for m in matches}
        seen_keys = {dedup_key(m) for m in matches}
        broader_filters = {k: v for k, v in filters.items() if k != "type-category"}
        broader_matches, _, _ = _fetch_matches(token, start, end, broader_filters, max_pages)
        for fields in broader_matches:
            if fields["url"] in seen_urls or dedup_key(fields) in seen_keys:
                continue
            if matches_type_category_text(fields, type_category):
                matches.append(fields)
                seen_urls.add(fields["url"])
                seen_keys.add(dedup_key(fields))
                keyword_matches_added += 1

    matches.sort(key=lambda f: f["parsed_date"])

    full_desc_fetched = 0
    full_desc_unavailable = 0
    if full_description and matches:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fulls = list(pool.map(fetch_full_description, [m["url"] for m in matches]))
        for m, full in zip(matches, fulls):
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
        "type_category_keyword_matches_added": keyword_matches_added,
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
