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
    include_full_description: bool = True,
    save_csv_path: str | None = None,
) -> dict:
    """Fetch ServiceNow Community events (SNUGs, webinars, World Forum stops, etc.)
    within a date range and return them as structured data.

    Args:
        start_date: Inclusive start of the date range, ISO format YYYY-MM-DD.
        end_date: Inclusive end of the date range, ISO format YYYY-MM-DD.
        status_category: Optional filter slug. Only "upcoming" is confirmed to work;
            other values frequently 400 -- if one fails, drop it and retry without it
            rather than guessing an alternate spelling.
        location_category: Optional filter slug, e.g. "in-person", "virtual", "hybrid".
        product_category: Optional filter slug, e.g. "app-engine". The full product
            list is long (from the UI's Product filter) -- pass whatever slug the
            caller gives, or slugify a plain label (lowercase, spaces to hyphens).
        type_category: Optional filter slug, e.g. "webinar", "workshop", "academy",
            "office-hours", "360-exchange".
        include_full_description: If true (default), fetch each matched event's own
            detail page to get the full description instead of the listing's
            "..."-truncated blurb. Costs one extra HTTP request per matched event --
            set to false for date ranges wide enough that this would be slow (a few
            dozen events is quick; a few hundred will take a while, sequentially).
        save_csv_path: If given, also write the results to a CSV file at this path
            (columns: category, title, description, date_text, parsed_date, location,
            url). Embedded line breaks in text fields are rendered as literal `<br>`
            rather than real newlines, so each event stays on one CSV row.

    Returns a dict with `events` (list of event dicts) and `meta` (counts: how many
    events matched, how many full descriptions were/weren't fetched, etc. -- surface
    anything notable from this to the user rather than silently discarding it).
    """
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date, "end_date")
    if start > end:
        start, end = end, start

    filters = {}
    if status_category:
        filters["status-category"] = status_category
    if location_category:
        filters["location-category"] = location_category
    if product_category:
        filters["product-category"] = product_category
    if type_category:
        filters["type-category"] = type_category

    events, meta = core.fetch_events(start, end, filters, full_description=include_full_description)

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
