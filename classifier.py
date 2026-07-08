"""Classify emails into labels using the Gemini API."""

import json
import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class RateLimited(Exception):
    """Raised when Gemini keeps rate-limiting us (e.g. the free daily cap).

    Not an error to crash on — the caller stops cleanly and the user reruns
    later to resume where it left off.
    """


class GeminiClassifier:
    def __init__(self, api_key: str, model: str, labels: list[dict]):
        self.api_key = api_key
        self.model = model
        self.labels = labels
        self.label_names = {l["name"] for l in labels}

    def classify_batch(self, emails: list[dict]) -> list[str | None]:
        """Classify a batch of emails.

        Each email is a dict with keys: sender, subject, body.
        Returns one label name (or None if unclassifiable) per email, in order.
        """
        if not emails:
            return []

        label_lines = "\n".join(
            f'- "{l["name"]}": {l["description"]}' for l in self.labels
        )
        email_lines = "\n\n".join(
            f"EMAIL {i}\nFrom: {e['sender']}\nSubject: {e['subject']}\nBody: {e['body']}"
            for i, e in enumerate(emails)
        )
        prompt = (
            "You are an email classifier. Assign exactly one label to each email "
            "from this list:\n"
            f"{label_lines}\n\n"
            "Respond with a JSON array of objects, one per email, in the same "
            'order: [{"index": <email number>, "label": "<label name>"}]. '
            'Use exactly the label names given. If no label fits, use "label": null.\n\n'
            f"{email_lines}"
        )

        data = self._request(prompt)

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            items = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Unexpected Gemini response: {e}\n{data}") from e

        results: list[str | None] = [None] * len(emails)
        for item in items:
            idx = item.get("index")
            label = item.get("label")
            if isinstance(idx, int) and 0 <= idx < len(emails):
                results[idx] = label if label in self.label_names else None
        return results

    def _request(self, prompt: str) -> dict:
        """Call Gemini, retrying with backoff on rate limits and server errors."""
        delays = [15, 30, 60, 120]
        for attempt in range(len(delays) + 1):
            resp = requests.post(
                GEMINI_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0,
                    },
                },
                timeout=120,
            )
            if resp.status_code in (429, 500, 503):
                if attempt < len(delays):
                    wait = delays[attempt]
                    print(f"  (Gemini returned {resp.status_code}, retrying in {wait}s...)")
                    time.sleep(wait)
                    continue
                if resp.status_code == 429:
                    # Still rate-limited after all retries — likely the daily cap.
                    raise RateLimited(
                        "Gemini is still rate-limiting after several retries "
                        "(probably the free-tier daily limit)."
                    )
            resp.raise_for_status()
            return resp.json()
