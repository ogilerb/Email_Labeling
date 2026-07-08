"""Outlook: fetch inbox emails and apply categories via Microsoft Graph."""

import atexit
import os

import msal
import requests

SCOPES = ["Mail.ReadWrite"]
TOKEN_FILE = "token_outlook.json"
GRAPH = "https://graph.microsoft.com/v1.0"

# Colors cycled through when creating new categories (preset0=red ... preset24).
CATEGORY_COLORS = [f"preset{i}" for i in range(25)]


class OutlookClient:
    def __init__(self, client_id: str, authority: str):
        if not client_id:
            raise ValueError(
                "outlook.client_id is empty in config.yaml. "
                "Create an Azure app registration first (see README)."
            )
        self.token = self._auth(client_id, authority)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _auth(self, client_id: str, authority: str) -> str:
        cache = msal.SerializableTokenCache()
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                cache.deserialize(f.read())

        def save_cache():
            if cache.has_state_changed:
                with open(TOKEN_FILE, "w") as f:
                    f.write(cache.serialize())

        atexit.register(save_cache)

        app = msal.PublicClientApplication(
            client_id, authority=authority, token_cache=cache
        )
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Failed to start device flow: {flow}")
            print(f"\n[Outlook] {flow['message']}\n")
            result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Outlook auth failed: {result.get('error_description')}")
        save_cache()
        return result["access_token"]

    def ensure_categories(self, names: list[str]) -> None:
        """Create any of our categories that don't exist yet (so they get colors)."""
        resp = requests.get(
            f"{GRAPH}/me/outlook/masterCategories", headers=self.headers, timeout=30
        )
        resp.raise_for_status()
        existing = {c["displayName"] for c in resp.json().get("value", [])}
        for i, name in enumerate(names):
            if name in existing:
                continue
            requests.post(
                f"{GRAPH}/me/outlook/masterCategories",
                headers=self.headers,
                json={"displayName": name, "color": CATEGORY_COLORS[i % len(CATEGORY_COLORS)]},
                timeout=30,
            ).raise_for_status()

    def iter_emails(self, max_results: int | None, our_labels: set[str]):
        """Yield inbox emails that don't already have one of our categories.

        This is a generator: it yields emails one at a time as it pages through
        the inbox, so the caller can label them incrementally instead of waiting
        for the whole inbox to download first.

        max_results=None means no limit (walk the entire inbox).
        Each yielded item: {id, sender, subject, body, categories}.
        """
        yielded = 0
        url = f"{GRAPH}/me/mailFolders/inbox/messages"
        params = {
            "$top": 100,
            "$select": "id,subject,from,bodyPreview,categories",
            "$orderby": "receivedDateTime desc",
        }
        while url:
            resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for msg in data.get("value", []):
                if our_labels & set(msg.get("categories", [])):
                    continue  # already labeled by us
                sender = (msg.get("from") or {}).get("emailAddress", {})
                yield {
                    "id": msg["id"],
                    "sender": f"{sender.get('name', '')} <{sender.get('address', '')}>",
                    "subject": msg.get("subject", ""),
                    "body": msg.get("bodyPreview", ""),
                    "categories": msg.get("categories", []),
                }
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            url = data.get("@odata.nextLink")
            params = None  # nextLink already includes the query params

    def apply_label(self, message_id: str, label_name: str, existing: list[str]) -> None:
        requests.patch(
            f"{GRAPH}/me/messages/{message_id}",
            headers=self.headers,
            json={"categories": existing + [label_name]},
            timeout=30,
        ).raise_for_status()
