"""Classify emails into labels using the Gemini API."""

import json
import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class RateLimited(Exception):
    """Signals the run should stop cleanly and be resumed later.

    Raised when Gemini keeps rate-limiting us (e.g. the free daily cap) or the
    network keeps failing after several retries. Not an error to crash on — the
    caller stops cleanly and the user reruns later to resume where it left off.
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

        If Gemini's safety filter blocks the whole batch (some old spam trips
        PROHIBITED_CONTENT, which can't be disabled), we recursively split the
        batch to isolate the offending email(s) and skip only those, so one bad
        email never sinks the rest.
        """
        if not emails:
            return []

        data = self._request(self._build_prompt(emails))
        text = self._extract_text(data)

        if text is None:
            # Blocked or empty response.
            if len(emails) == 1:
                print(f"  - skip (blocked by safety filter): {emails[0]['subject'][:50]}")
                return [None]
            mid = len(emails) // 2
            return self.classify_batch(emails[:mid]) + self.classify_batch(emails[mid:])

        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Gemini returned non-JSON: {e}\n{text}") from e

        results: list[str | None] = [None] * len(emails)
        for item in items:
            idx = item.get("index")
            label = item.get("label")
            if isinstance(idx, int) and 0 <= idx < len(emails):
                results[idx] = label if label in self.label_names else None
        return results

    def _build_prompt(self, emails: list[dict]) -> str:
        label_lines = "\n".join(
            f'- "{l["name"]}": {l["description"]}' for l in self.labels
        )
        email_lines = "\n\n".join(
            f"EMAIL {i}\nFrom: {e['sender']}\nSubject: {e['subject']}\nBody: {e['body']}"
            for i, e in enumerate(emails)
        )
        return (
            "You are an email classifier. Assign exactly one label to each email "
            "from this list:\n"
            f"{label_lines}\n\n"
            "Respond with a JSON array of objects, one per email, in the same "
            'order: [{"index": <email number>, "label": "<label name>"}]. '
            'Use exactly the label names given. If no label fits, use "label": null.\n\n'
            f"{email_lines}"
        )

    @staticmethod
    def _extract_text(data: dict) -> str | None:
        """Pull the model's text out of a Gemini response.

        Returns None if the response was blocked or has no usable text, so the
        caller can fall back to splitting the batch.
        """
        candidates = data.get("candidates")
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts")
        if not parts:
            return None
        return parts[0].get("text")

    def _request(self, prompt: str) -> dict:
        """Call Gemini, retrying with backoff on rate limits, server errors,
        and transient network failures (timeouts, dropped connections)."""
        delays = [15, 30, 60, 120]
        for attempt in range(len(delays) + 1):
            try:
                resp = requests.post(
                    GEMINI_URL.format(model=self.model),
                    headers={"x-goog-api-key": self.api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "temperature": 0,
                        },
                        # We're only reading marketing/spam to sort it, so turn
                        # off the configurable safety blocks. (PROHIBITED_CONTENT
                        # is not configurable and is handled by splitting.)
                        "safetySettings": [
                            {"category": c, "threshold": "BLOCK_NONE"}
                            for c in (
                                "HARM_CATEGORY_HARASSMENT",
                                "HARM_CATEGORY_HATE_SPEECH",
                                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                                "HARM_CATEGORY_DANGEROUS_CONTENT",
                            )
                        ],
                    },
                    timeout=120,
                )
            except requests.exceptions.RequestException as e:
                # Network trouble (timeout, connection reset, DNS, ...): retry.
                if attempt < len(delays):
                    wait = delays[attempt]
                    print(f"  (network error: {type(e).__name__}, retrying in {wait}s...)")
                    time.sleep(wait)
                    continue
                raise RateLimited(
                    f"Network keeps failing ({type(e).__name__}) after several "
                    "retries. Check your connection and rerun to resume."
                ) from e

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
