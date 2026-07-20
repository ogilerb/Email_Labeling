"""Gmail: fetch inbox emails and apply labels via the Gmail API."""

import base64
import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = "token_gmail.json"


class GmailClient:
    def __init__(self, credentials_file: str):
        self.service = build("gmail", "v1", credentials=self._auth(credentials_file))
        self._label_ids: dict[str, str] = {}  # label name -> id

    def _auth(self, credentials_file: str) -> Credentials:
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            refreshed = False
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    refreshed = True
                except RefreshError:
                    # Stored refresh token was revoked or expired: discard it
                    # and fall through to a fresh interactive login.
                    creds = None
            if not refreshed:
                if not os.path.exists(credentials_file):
                    raise FileNotFoundError(
                        f"Gmail OAuth client file not found: {credentials_file}. "
                        "Download it from Google Cloud Console (see README)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return creds

    def ensure_labels(self, names: list[str]) -> None:
        """Create any missing labels and cache all label ids."""
        existing = self.service.users().labels().list(userId="me").execute()
        by_name = {l["name"]: l["id"] for l in existing.get("labels", [])}
        for name in names:
            if name in by_name:
                self._label_ids[name] = by_name[name]
            else:
                created = (
                    self.service.users()
                    .labels()
                    .create(userId="me", body={"name": name})
                    .execute()
                )
                self._label_ids[name] = created["id"]

    def iter_emails(self, query: str, max_results: int | None):
        """Yield emails matching the query that don't already have one of our labels.

        This is a generator: it yields emails one at a time as it pages through
        the mailbox, so the caller can label them incrementally instead of
        waiting for the whole mailbox to download first.

        max_results=None means no limit (walk the entire mailbox for the query).
        Each yielded item: {id, sender, subject, body}.
        """
        # Exclude already-labeled emails at the query level, so a resumed run
        # doesn't re-download everything it labeled on previous runs.
        exclusions = " ".join(f'-label:"{name}"' for name in self._label_ids)
        full_query = f"{query} {exclusions}".strip()

        our_ids = set(self._label_ids.values())
        yielded = 0
        page_token = None
        while True:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=full_query, maxResults=500, pageToken=page_token)
                .execute()
            )
            for ref in resp.get("messages", []):
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=ref["id"], format="full")
                    .execute()
                )
                if our_ids & set(msg.get("labelIds", [])):
                    continue  # already labeled by us (belt-and-suspenders)
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                body = _extract_text(msg.get("payload", {})) or msg.get("snippet", "")
                yield {
                    "id": msg["id"],
                    "sender": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "body": body,
                }
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def apply_label(self, message_id: str, label_name: str) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [self._label_ids[label_name]]},
        ).execute()


def _extract_text(payload: dict) -> str:
    """Pull the first text/plain body out of a (possibly nested) MIME payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_text(part)
        if text:
            return text
    return ""
