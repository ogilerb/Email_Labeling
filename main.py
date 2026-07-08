"""Label Gmail and Outlook emails using Gemini.

Usage:
    python main.py                 # label both accounts
    python main.py --gmail         # Gmail only
    python main.py --outlook       # Outlook only
    python main.py --dry-run       # show what would be labeled without changing anything
    python main.py --max 50        # override max emails per account
    python main.py --all           # process the entire mailbox history (no limit)
"""

import argparse
import os
import sys

import yaml

from classifier import GeminiClassifier, RateLimited


def load_env_file(path: str = ".env") -> None:
    """Minimal .env loader (KEY=value lines)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())  # collapse whitespace
    return text[:limit]


def batched(iterable, size):
    """Yield lists of up to `size` items from an iterable (lazily)."""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def classify_and_apply(provider_name, emails, classifier, apply_fn, prep_fn, batch_size, dry_run):
    """Consume `emails` (an iterator) in batches, classifying and labeling as we go.

    Labels are applied incrementally, so interrupting (Ctrl+C) or hitting the
    Gemini rate limit keeps everything labeled so far — rerunning resumes.
    """
    labeled = skipped = seen = 0
    try:
        for batch in batched(emails, batch_size):
            for e in batch:
                prep_fn(e)
            labels = classifier.classify_batch(batch)
            seen += len(batch)
            for email, label in zip(batch, labels):
                subject = truncate(email["subject"], 60) or "(no subject)"
                if label is None:
                    print(f"  - skip: {subject}")
                    skipped += 1
                    continue
                if dry_run:
                    print(f"  - would label [{label}]: {subject}")
                else:
                    apply_fn(email, label)
                    print(f"  - labeled [{label}]: {subject}")
                labeled += 1
            if seen % 100 == 0:
                print(f"[{provider_name}] progress: {seen} processed so far...")
    except RateLimited as e:
        print(f"\n[{provider_name}] Stopping: {e}")
        print(f"[{provider_name}] Rerun the same command later to pick up where it left off.")
    except KeyboardInterrupt:
        print(f"\n[{provider_name}] Interrupted. Labels already applied are kept; rerun to resume.")

    if seen == 0:
        print(f"[{provider_name}] No new emails to label.")
        return
    action = "would be labeled" if dry_run else "labeled"
    print(f"[{provider_name}] Done: {labeled} {action}, {skipped} skipped ({seen} processed).")


def resolve_max(config, args) -> int | None:
    if args.all:
        return None
    return args.max or config["run"]["max_emails"]


def run_gmail(config, classifier, args):
    from gmail_client import GmailClient

    print("[Gmail] Connecting...")
    client = GmailClient(config["gmail"]["credentials_file"])
    label_names = [l["name"] for l in config["labels"]]
    client.ensure_labels(label_names)
    body_chars = config["run"]["body_chars"]
    emails = client.iter_emails(
        query=config["gmail"].get("query", "in:inbox"),
        max_results=resolve_max(config, args),
    )
    classify_and_apply(
        "Gmail",
        emails,
        classifier,
        lambda email, label: client.apply_label(email["id"], label),
        lambda email: email.update(body=truncate(email["body"], body_chars)),
        config["run"]["batch_size"],
        args.dry_run,
    )


def run_outlook(config, classifier, args):
    from outlook_client import OutlookClient

    print("[Outlook] Connecting...")
    client = OutlookClient(
        config["outlook"]["client_id"],
        config["outlook"].get("authority", "https://login.microsoftonline.com/consumers"),
    )
    label_names = [l["name"] for l in config["labels"]]
    client.ensure_categories(label_names)
    body_chars = config["run"]["body_chars"]
    emails = client.iter_emails(
        max_results=resolve_max(config, args),
        our_labels=set(label_names),
    )
    classify_and_apply(
        "Outlook",
        emails,
        classifier,
        lambda email, label: client.apply_label(email["id"], label, email["categories"]),
        lambda email: email.update(body=truncate(email["body"], body_chars)),
        config["run"]["batch_size"],
        args.dry_run,
    )


def main():
    parser = argparse.ArgumentParser(description="Label emails with Gemini.")
    parser.add_argument("--gmail", action="store_true", help="process Gmail only")
    parser.add_argument("--outlook", action="store_true", help="process Outlook only")
    parser.add_argument("--dry-run", action="store_true", help="don't apply labels")
    parser.add_argument("--max", type=int, help="max emails per account")
    parser.add_argument(
        "--all", action="store_true", help="process all emails (ignore max limit)"
    )
    parser.add_argument("--config", default="config.yaml", help="config file path")
    args = parser.parse_args()

    load_env_file()
    with open(args.config) as f:
        config = yaml.safe_load(f)

    api_key = os.environ.get("GEMINI_API_KEY") or config["gemini"].get("api_key")
    if not api_key:
        sys.exit(
            "No Gemini API key. Set GEMINI_API_KEY (env or .env file) or "
            "gemini.api_key in config.yaml. Get one at https://aistudio.google.com/apikey"
        )

    classifier = GeminiClassifier(api_key, config["gemini"]["model"], config["labels"])

    # No flag = both (whatever is enabled in config); flags narrow it down.
    do_gmail = args.gmail or (not args.outlook and config["gmail"].get("enabled", True))
    do_outlook = args.outlook or (not args.gmail and config["outlook"].get("enabled", True))

    if do_gmail:
        run_gmail(config, classifier, args)
    if do_outlook:
        run_outlook(config, classifier, args)


if __name__ == "__main__":
    main()
