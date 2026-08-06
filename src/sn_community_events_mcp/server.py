"""
MCP server exposing ServiceNow Community events
(https://www.servicenow.com/community/events/ct-p/TopLevel_Events) as a tool.

Built as an MCP server rather than a Claude-Desktop-invoked skill script because
Desktop's sandboxed code-execution environment sits behind an org-managed network
egress allowlist that blocks servicenow.com (x-deny-reason: host_not_allowed). An
MCP server, by contrast, runs as a local subprocess on the user's own machine and
uses its real network stack, so it isn't subject to that sandbox's allowlist.

All the actual fetch/parse/pagination logic lives in core.py -- see that module's
docstrings for the API's various gotchas (ignored `page` param, keyset cursor
pagination, unreliable total-results, etc.) before changing anything here.
"""
from datetime import date

from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP("sn-community-events")


def _parse_iso_date(value, field_name):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO date (YYYY-MM-DD), got: {value!r}")


@mcp.tool()
def get_community_events(
    start_date: str,
    end_date: str,
    status_category: str | None = None,
    location_category: str | None = None,
    product_category: str | None = None,
    type_category: str | None = None,
    include_full_description: bool = False,
    save_csv_path: str | None = None,
) -> dict:
    """Fetch ServiceNow Community events (SNUGs, webinars, World Forum stops, etc.)
    within a date range and return them as structured data.

    Args:
        start_date: Inclusive start of the date range, ISO format YYYY-MM-DD.
        end_date: Inclusive end of the date range, ISO format YYYY-MM-DD.
        status_category: Optional filter value: "ongoing", "upcoming", "past", or
            "on-demand".
        location_category: Optional filter value: "In Person", "Virtual", or "Hybrid".
            The real facet values are exact-case Title strings with spaces, not
            lowercase-hyphenated slugs -- a natural guess like "in-person" is
            normalized onto "In Person" automatically, so don't bother pre-slugifying.
        product_category: Optional filter slug, e.g. "app-engine". The full product
            list is long (from the UI's Product filter) -- pass whatever slug the
            caller gives, or slugify a plain label (lowercase, spaces to hyphens).
        type_category: Optional filter value: "Webinar", "360 Exchange",
            "Office Hours", "Workshop", "Academy", "SNUGs", "Ask the Experts", or
            "Developer Meetups" (also normalized from natural guesses like "webinar"
            or "snugs"). This facet is independent of an event's own community-group
            name and is sparsely/inconsistently applied -- e.g. plenty of events
            posted under a "<City> SNUG" group are NOT tagged type-category=SNUGs
            upstream, and an event may carry zero or multiple type tags. To
            compensate, results are supplemented with a client-side keyword match of
            this value against each event's category/title text; see
            `meta.type_category_keyword_matches_added` for how many that added.
        include_full_description: If true, fetch each matched event's own detail page
            to get the full description instead of the listing's "..."-truncated
            blurb (fetched concurrently, but still one extra HTTP request per matched
            event). Defaults to false -- set true only when full descriptions are
            actually needed, since a wide date range can match hundreds of events.
        save_csv_path: If given, also write the results to a CSV file at this path
            (columns: category, title, description, date_text, parsed_date, location,
            url). Embedded line breaks in text fields are rendered as literal `<br>`
            rather than real newlines, so each event stays on one CSV row.

    Returns a dict with `events` (list of event dicts) and `meta` (counts: how many
    events matched, how many full descriptions were/weren't fetched, how many were
    added by the type-category keyword supplement, etc. -- surface anything notable
    from this to the user rather than silently discarding it).

    Suggested default presentation (a starting point, not a requirement -- always
    defer to whatever format the user actually asks for):
        - List view: for each event show the title (linked to `url` if presenting
          somewhere links render), a `category` tag/pill, and the date. Add a
          virtual/in-person pill from `location_type` when it's "virtual" or
          "in-person"; when it's "unknown", omit that pill rather than guessing.
          Always show the title -- don't collapse to a bare link or omit it for
          "compact" layouts.
        - Calendar view: put the event title as visible text on its date by default,
          not just a marker dot with the title deferred to a legend or a follow-up
          question -- only fall back to a dot/count-per-day if there are enough
          events on one day that titles would overlap.
    """
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date, "end_date")
    if start > end:
        start, end = end, start

    filters = {}
    if status_category:
        filters["status-category"] = core.normalize_facet_value(
            status_category, core.STATUS_CATEGORY_VALUES
        )
    if location_category:
        filters["location-category"] = core.normalize_facet_value(
            location_category, core.LOCATION_CATEGORY_VALUES
        )
    if product_category:
        filters["product-category"] = product_category
    if type_category:
        filters["type-category"] = core.normalize_facet_value(
            type_category, core.TYPE_CATEGORY_VALUES
        )

    events, meta = core.fetch_events(start, end, filters, full_description=include_full_description)

    meta["source"] = core.SOURCE_URL
    meta["source_attribution_hint"] = (
        "Cite this the way a web search result would be cited -- e.g. a trailing "
        '"Source: ServiceNow Community Events" line linking to meta.source -- rather '
        "than presenting the data with no attribution."
    )

    meta["presentation_hint"] = (
        "Default (override freely if the user asks for something different): in lists, "
        "always show each event's title, its category tag, and its date; add a "
        "virtual/in-person tag from location_type only when it's not \"unknown\". In "
        "calendar views, show the title as visible text on its date, not just a marker "
        "-- don't wait for the user to ask before including it."
    )

    if not include_full_description:
        meta["note"] = (
            "Each event's `description` is the listing's short, \"...\"-truncated blurb -- "
            "full descriptions exist but were not fetched because include_full_description "
            "defaults to false. Re-call with include_full_description=true to get them "
            "(one extra request per matched event, fetched concurrently)."
        )

    if save_csv_path:
        core.write_csv(events, save_csv_path)
        meta["csv_path"] = save_csv_path

    out_events = [
        {**e, "parsed_date": e["parsed_date"].isoformat()}
        for e in events
    ]
    return {"events": out_events, "meta": meta}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
