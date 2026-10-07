"""
gmail_fetcher.py

Fetch emails from a Gmail account using the Gmail API (google-api-python-client),
with a flexible filter builder that supports (almost) every Gmail search operator.

SETUP
-----
1. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

2. Go to https://console.cloud.google.com/ -> create/select a project ->
   enable the "Gmail API" -> create OAuth 2.0 Client ID (type: Desktop app) ->
   download the JSON and save it as "credentials.json" in the project working directory.

3. Run the script once; a browser window will open for you to log in and
   consent. A "token_gmail.json" file will be saved so you don't have to log in
   again on future runs.

USAGE
-----
    from host_helper.gmail_fetcher import get_emails, build_query

    # Simple: last 20 unread emails from a specific sender
    query = build_query(from_="sender@example.com", is_unread=True)
    emails = get_emails(query=query, max_results=20)

    # Or just pass a raw Gmail search string yourself
    emails = get_emails(query="from:sender@example.com has:attachment newer_than:7d")

    for e in emails:
        print(e["date"], e["from"], e["subject"])
"""

import base64
import os
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read-only scope is enough for fetching/searching emails.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token_gmail.json"  # separate from calendar_sync.py's token.json --
# same OAuth client (credentials.json), but each API's token is scoped
# differently and must be cached independently, or one overwrites the other.


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def get_gmail_service():
    """Authenticate (using cached token if available) and return a Gmail API service object."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# --------------------------------------------------------------------------
# Query builder — covers all major Gmail search operators
# --------------------------------------------------------------------------


def build_query(
    from_: Optional[str] = None,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    subject: Optional[str] = None,
    has_words: Optional[str] = None,  # free-text search (body/subject)
    exact_phrase: Optional[str] = None,  # wrapped in quotes
    exclude_words: Optional[str] = None,  # space-separated words to exclude
    label: Optional[str] = None,
    category: Optional[str] = None,  # primary, social, promotions, updates, forums
    is_unread: Optional[bool] = None,
    is_read: Optional[bool] = None,
    is_starred: Optional[bool] = None,
    is_important: Optional[bool] = None,
    is_snoozed: Optional[bool] = None,
    has_attachment: Optional[bool] = None,
    filename: Optional[str] = None,  # e.g. "pdf" or "report.xlsx"
    in_: Optional[str] = None,  # inbox, trash, spam, anywhere, sent, draft, chat
    after: Optional[str] = None,  # "YYYY/MM/DD"
    before: Optional[str] = None,  # "YYYY/MM/DD"
    older_than: Optional[str] = None,  # e.g. "1d", "2m", "1y"
    newer_than: Optional[str] = None,  # e.g. "7d", "1m"
    larger_than: Optional[str] = None,  # e.g. "10M", "500K"
    smaller_than: Optional[str] = None,  # e.g. "1M"
    list_: Optional[str] = None,  # mailing list address
    deliveredto: Optional[str] = None,
    rfc822msgid: Optional[str] = None,
    has_drive: Optional[bool] = None,  # attached Google Drive file
    has_document: Optional[bool] = None,  # attached Google Doc
    has_spreadsheet: Optional[bool] = None,
    has_presentation: Optional[bool] = None,
    has_youtube: Optional[bool] = None,
    raw_extra: Optional[str] = None,  # any extra raw query snippet to append
) -> str:
    """
    Build a Gmail search query string from structured filter options.
    Any parameter left as None is simply omitted. Combine with raw_extra
    for anything not covered here, or bypass this entirely and write
    your own Gmail search string directly.
    """
    parts: List[str] = []

    def add(op: str, val: Optional[str], quote: bool = False):
        if val is not None:
            v = f'"{val}"' if quote else val
            parts.append(f"{op}:{v}")

    add("from", from_)
    add("to", to)
    add("cc", cc)
    add("bcc", bcc)
    add("subject", subject, quote=" " in subject if subject else False)
    add("label", label)
    add("category", category)
    add("filename", filename)
    add("in", in_)
    add("after", after)
    add("before", before)
    add("older_than", older_than)
    add("newer_than", newer_than)
    add("larger", larger_than)
    add("smaller", smaller_than)
    add("list", list_)
    add("deliveredto", deliveredto)
    add("rfc822msgid", rfc822msgid)

    if exact_phrase:
        parts.append(f'"{exact_phrase}"')
    if has_words:
        parts.append(has_words)
    if exclude_words:
        for word in exclude_words.split():
            parts.append(f"-{word}")

    if is_unread:
        parts.append("is:unread")
    if is_read:
        parts.append("is:read")
    if is_starred:
        parts.append("is:starred")
    if is_important:
        parts.append("is:important")
    if is_snoozed:
        parts.append("is:snoozed")
    if has_attachment:
        parts.append("has:attachment")
    if has_drive:
        parts.append("has:drive")
    if has_document:
        parts.append("has:document")
    if has_spreadsheet:
        parts.append("has:spreadsheet")
    if has_presentation:
        parts.append("has:presentation")
    if has_youtube:
        parts.append("has:youtube")

    if raw_extra:
        parts.append(raw_extra)

    return " ".join(parts)


# --------------------------------------------------------------------------
# Fetching emails
# --------------------------------------------------------------------------


def _get_header(headers: List[Dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(payload: Dict[str, Any]) -> str:
    """Recursively pull the plain-text (falling back to HTML) body out of a message payload."""
    if payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("UTF-8", errors="replace")

    parts = payload.get("parts", [])
    text_plain, text_html = "", ""
    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            text_plain += base64.urlsafe_b64decode(part["body"]["data"].encode("UTF-8")).decode(
                "UTF-8", errors="replace"
            )
        elif mime == "text/html" and part.get("body", {}).get("data"):
            text_html += base64.urlsafe_b64decode(part["body"]["data"].encode("UTF-8")).decode(
                "UTF-8", errors="replace"
            )
        elif part.get("parts"):
            nested = _extract_body(part)
            if nested:
                text_plain += nested

    return text_plain or text_html


def get_emails(
    query: str = "",
    max_results: int = 50,
    include_body: bool = False,
    service=None,
) -> List[Dict[str, Any]]:
    """
    Fetch emails matching a Gmail search query.

    Args:
        query: A Gmail search string. Build one with build_query(...) or
               write one directly (e.g. "from:sender@example.com is:unread newer_than:3d").
        max_results: Maximum number of emails to return.
        include_body: If True, also fetch and decode the message body
                      (slower, one extra API call per message).
        service: Optional pre-built Gmail service object (to reuse a session).

    Returns:
        A list of dicts, one per email, with keys:
        id, thread_id, date, from, to, subject, snippet, labels, and
        optionally body.
    """
    if service is None:
        service = get_gmail_service()

    results = []
    page_token = None

    while len(results) < max_results:
        remaining = max_results - len(results)
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(remaining, 100),
                pageToken=page_token,
            )
            .execute()
        )

        message_stubs = resp.get("messages", [])
        if not message_stubs:
            break

        for stub in message_stubs:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="full" if include_body else "metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute()
            )

            headers = msg.get("payload", {}).get("headers", [])
            email_data = {
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "date": _get_header(headers, "Date"),
                "from": _get_header(headers, "From"),
                "to": _get_header(headers, "To"),
                "subject": _get_header(headers, "Subject"),
                "snippet": msg.get("snippet", ""),
                "labels": msg.get("labelIds", []),
            }

            if include_body:
                email_data["body"] = _extract_body(msg.get("payload", {}))

            results.append(email_data)
            if len(results) >= max_results:
                break

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


# --------------------------------------------------------------------------
# Example usage
# --------------------------------------------------------------------------

if __name__ == "__main__":
    service = get_gmail_service()

    # Example: unread emails from the last 7 days with an attachment
    query = build_query(is_unread=True, newer_than="7d", has_attachment=True)
    print(f"Query: {query}")

    emails = get_emails(query=query, max_results=10, include_body=False, service=service)

    for e in emails:
        print(f"[{e['date']}] {e['from']} -> {e['subject']}")
