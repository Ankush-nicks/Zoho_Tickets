"""
Zoho Creator integration.

Talks to a Zoho Creator "Invoke URL" (custom API) to fetch a ticket record
by id, then pulls out the fields the rest of the app cares about. Every
Zoho-specific detail (URL, auth header, field names) lives in config.py as
env vars, on purpose - none of it is confirmed against a real Zoho account
yet, so nothing about the actual request/response shape is hardcoded here.
"""

import httpx

from . import config


class ZohoError(Exception):
    """Raised when the Zoho invoke call fails or the response can't be parsed."""


def _find_field(data, field_name: str):
    """
    Breadth-first search for `field_name` anywhere in a nested dict/list
    response, returning the first match. Zoho custom APIs commonly wrap the
    actual record a level or two down (e.g. under "result" or "data") in a
    shape we haven't confirmed yet, so searching beats guessing one exact path.
    """
    queue = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if field_name in node:
                return node[field_name]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def fetch_ticket_raw(ticket_id: str) -> dict:
    """Call the Zoho invoke URL for `ticket_id` and return the parsed JSON response."""
    if "{ticket_id}" not in config.ZOHO_INVOKE_URL:
        raise ZohoError("ZOHO_INVOKE_URL must contain a {ticket_id} placeholder.")
    url = config.ZOHO_INVOKE_URL.format(ticket_id=ticket_id)

    try:
        resp = httpx.get(
            url,
            headers={config.ZOHO_AUTH_HEADER_NAME: config.ZOHO_API_KEY},
            timeout=15.0,
        )
    except httpx.RequestError as e:
        raise ZohoError(f"Could not reach Zoho invoke URL: {e}") from e

    if resp.status_code != 200:
        raise ZohoError(f"Zoho invoke URL returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        return resp.json()
    except ValueError as e:
        raise ZohoError(f"Zoho invoke URL did not return valid JSON: {e}") from e


def get_ticket_issue(ticket_id: str) -> dict:
    """
    Fetch a ticket from Zoho and extract the fields the app needs.
    Returns {"ticket_id", "issue_in_detail", "raw"} - "raw" is the full
    untouched response, kept around for the debug page and for diagnosing
    field-name mismatches once real data is available.
    """
    raw = fetch_ticket_raw(ticket_id)
    issue_in_detail = _find_field(raw, config.ZOHO_FIELD_ISSUE_DETAIL)
    found_ticket_id = _find_field(raw, config.ZOHO_FIELD_TICKET_ID)
    return {
        "ticket_id": found_ticket_id if found_ticket_id is not None else ticket_id,
        "issue_in_detail": issue_in_detail,
        "raw": raw,
    }
