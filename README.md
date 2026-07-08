# Email Labeling

Automatically labels your Gmail and Outlook inbox emails using Gemini. Runs
locally — your emails go only to Google's Gemini API for classification.

- **Gmail**: applies real Gmail labels (creates them if missing)
- **Outlook**: applies categories (Outlook's version of labels), with colors
- Labels and their meanings are defined in `config.yaml` — edit them freely
- Already-labeled emails are skipped, so it's safe to run repeatedly

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Gemini API key

Get a free key at https://aistudio.google.com/apikey, then create a `.env`
file in this folder:

```
GEMINI_API_KEY=your-key-here
```

### 3. Gmail credentials

1. Go to https://console.cloud.google.com and create a project (any name).
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen** → External → fill in the app
   name and your email. Under **Audience → Test users**, add
   `olliegoodstone@gmail.com`.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   Application type: **Desktop app**.
5. Download the JSON and save it in this folder as `credentials.json`.

First run opens a browser window to sign in; the token is then saved to
`token_gmail.json` and reused.

### 4. Outlook credentials

1. Go to https://portal.azure.com → **Microsoft Entra ID → App registrations
   → New registration**.
2. Name it anything. Under supported account types choose
   **Personal Microsoft accounts only** (or "Accounts in any organizational
   directory and personal Microsoft accounts" if it's a work address).
3. After creating: **Authentication → Advanced settings → Allow public client
   flows → Yes** → Save.
4. Copy the **Application (client) ID** from the Overview page into
   `config.yaml` under `outlook.client_id`.
   - If you chose the work/school option in step 2, also change
     `outlook.authority` to `https://login.microsoftonline.com/common`.

First run prints a code and a link (device sign-in); the token is then saved
to `token_outlook.json` and reused.

## Usage

```bash
python main.py --dry-run    # see what would be labeled, change nothing
python main.py              # label both accounts
python main.py --gmail      # Gmail only
python main.py --outlook    # Outlook only
python main.py --max 50     # process up to 50 emails per account
python main.py --all        # process your entire mailbox history
```

A big `--all` run labels emails incrementally as it goes and is fully
resumable. It can be interrupted (Ctrl+C) at any time — labels already applied
are kept, and rerunning skips them and continues with the rest. On the free
Gemini tier it will auto-stop when it hits the daily request limit and tell you
to rerun later; already-labeled emails are excluded from the next run's search,
so resuming is fast even with tens of thousands of emails.

### The "Can Delete" label

The program never deletes anything itself — it only labels. To clean up:
in Gmail, click the **Can Delete** label in the sidebar, review the list,
select all, and delete. Same idea in Outlook via the category filter.

## Customizing

Everything lives in `config.yaml`:

- **labels** — add/remove/rename labels; the `description` is what Gemini
  uses to decide, so make it specific.
- **gmail.query** — which emails to consider (Gmail search syntax, e.g.
  `in:inbox is:unread` or `in:inbox newer_than:7d`).
- **run.max_emails / batch_size / body_chars** — volume and cost controls.

## Running on a schedule (optional)

To label new mail every 30 minutes, add a cron entry (`crontab -e`):

```
*/30 * * * * cd "/path/to/Email_Labeling" && .venv/bin/python main.py >> label.log 2>&1
```

(Use the real path to this folder.) The saved tokens mean no browser prompt
is needed after the first interactive run.
