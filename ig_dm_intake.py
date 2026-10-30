#!/usr/bin/env python3
"""
Instagram DM intake splitter (official Instagram Messaging API via Graph API).

What it does
  1. Pulls Instagram DM conversations for a Facebook Page linked to a
     Professional Instagram account (optionally unread only).
  2. Assigns each conversation to exactly ONE intake person. The split is
     deterministic: the same conversation always goes to the same person, run
     after run, so threads never get split or duplicated across staff.
  3. Writes a separate CSV per intake person (and, optionally, a separate
     Google Sheet per person, shared ONLY with that person's email).

The access token lives only with whoever runs this script (the admin).
Intake staff never get the token or Instagram login; they get only their slice.

Setup
  pip install requests python-dotenv            # required
  pip install gspread google-auth               # only if using Google Sheets

  Create a .env file next to this script (never commit it):
    PAGE_ID=1234567890
    PAGE_ACCESS_TOKEN=EAAG...
    INTAKE_STAFF=alice@firm.com,bob@firm.com,carol@firm.com
    UNREAD_ONLY=true
    MAX_CONVERSATIONS=500
    GRAPH_VERSION=v23.0
    # Optional Google Sheets export:
    GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json

Run
  python ig_dm_intake.py
"""

import csv
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
PAGE_ID = os.getenv("PAGE_ID", "").strip()
ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
STAFF = [s.strip() for s in os.getenv("INTAKE_STAFF", "").split(",") if s.strip()]
UNREAD_ONLY = os.getenv("UNREAD_ONLY", "true").lower() in ("1", "true", "yes")
MAX_CONVERSATIONS = int(os.getenv("MAX_CONVERSATIONS", "500"))
MAX_MESSAGES_PER_CONVO = int(os.getenv("MAX_MESSAGES_PER_CONVO", "50"))
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "intake_exports"))

BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
CSV_FIELDS = [
    "assigned_to", "conversation_id", "customer_username", "customer_id",
    "unread_count", "conversation_updated", "message_time", "sender",
    "direction", "message",
]


# --------------------------------------------------------------------------- #
# Graph API helpers
# --------------------------------------------------------------------------- #
class GraphError(RuntimeError):
    pass


def graph_get(url, params=None, retries=4):
    """GET with retry on rate limits / transient errors."""
    params = dict(params or {})
    params.setdefault("access_token", ACCESS_TOKEN)
    for attempt in range(retries):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {"message": resp.text}
        code = err.get("code")
        # 4/17/32/613 = rate limiting; 1/2 = transient
        if code in (1, 2, 4, 17, 32, 613) and attempt < retries - 1:
            time.sleep(2 ** attempt * 5)
            continue
        raise GraphError(f"Graph API error {resp.status_code} (code {code}): {err.get('message')}")
    raise GraphError("Exhausted retries")


def paginate(url, params, limit):
    """Yield items across pages until `limit` items or no more pages."""
    count = 0
    data = graph_get(url, params)
    while True:
        for item in data.get("data", []):
            yield item
            count += 1
            if count >= limit:
                return
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        data = graph_get(next_url, {})  # next URL already carries params


def get_ig_account_id():
    """The Instagram business account ID linked to the Page (used to tell
    which messages were sent by the firm vs. by the customer)."""
    data = graph_get(f"{BASE}/{PAGE_ID}", {"fields": "instagram_business_account{id,username}"})
    acct = data.get("instagram_business_account")
    if not acct:
        raise GraphError("This Page has no linked Instagram Professional account.")
    return acct["id"], acct.get("username", "")


def fetch_conversations():
    params = {
        "platform": "instagram",
        "fields": "id,updated_time,unread_count,participants",
        "limit": 50,
    }
    # When filtering to unread, scan further than the cap, since some will be read.
    scan_limit = MAX_CONVERSATIONS * 4 if UNREAD_ONLY else MAX_CONVERSATIONS
    kept = []
    for convo in paginate(f"{BASE}/{PAGE_ID}/conversations", params, scan_limit):
        unread = convo.get("unread_count")
        if UNREAD_ONLY and unread is not None and int(unread) == 0:
            continue
        kept.append(convo)
        if len(kept) >= MAX_CONVERSATIONS:
            break
    return kept


def fetch_messages(convo_id):
    params = {"fields": "id,message,from,created_time", "limit": 25}
    return list(paginate(f"{BASE}/{convo_id}/messages", params, MAX_MESSAGES_PER_CONVO))


# --------------------------------------------------------------------------- #
# Assignment: each conversation -> exactly one staff member, stable over time
# --------------------------------------------------------------------------- #
def assign(convo_id, staff):
    h = int(hashlib.sha256(convo_id.encode()).hexdigest(), 16)
    return staff[h % len(staff)]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def safe_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def write_csvs(rows_by_staff, stamp):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = {}
    for person, rows in rows_by_staff.items():
        path = OUTPUT_DIR / f"intake_{safe_name(person)}_{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        try:
            os.chmod(path, 0o600)  # owner-only on the admin's machine
        except OSError:
            pass
        paths[person] = path
    return paths


def write_sheets(rows_by_staff, stamp):
    """One spreadsheet per person, shared only with that person."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    links = {}
    for person, rows in rows_by_staff.items():
        if "@" not in person:
            print(f"  skip Sheets for '{person}' (not an email address)")
            continue
        sh = gc.create(f"IG Intake - {person} - {stamp}")
        ws = sh.sheet1
        ws.update([CSV_FIELDS] + [[r.get(k, "") for k in CSV_FIELDS] for r in rows])
        sh.share(person, perm_type="user", role="writer", notify=True)
        links[person] = sh.url
    return links


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    missing = [n for n, v in (("PAGE_ID", PAGE_ID), ("PAGE_ACCESS_TOKEN", ACCESS_TOKEN)) if not v]
    if missing:
        sys.exit(f"Missing config: {', '.join(missing)} (set in .env)")
    if not STAFF:
        sys.exit("Set INTAKE_STAFF to a comma-separated list (emails if using Sheets).")

    ig_id, ig_username = get_ig_account_id()
    print(f"Instagram account: @{ig_username} ({ig_id})")

    convos = fetch_conversations()
    print(f"Conversations to export: {len(convos)} "
          f"({'unread only' if UNREAD_ONLY else 'all'})")

    rows_by_staff = {p: [] for p in STAFF}
    for i, convo in enumerate(convos, 1):
        cid = convo["id"]
        person = assign(cid, STAFF)
        participants = convo.get("participants", {}).get("data", [])
        customer = next((p for p in participants if p.get("id") != ig_id), {})
        try:
            messages = fetch_messages(cid)
        except GraphError as e:
            print(f"  [{i}] {cid}: could not fetch messages: {e}")
            messages = []
        for m in sorted(messages, key=lambda m: m.get("created_time", "")):
            sender = m.get("from", {})
            rows_by_staff[person].append({
                "assigned_to": person,
                "conversation_id": cid,
                "customer_username": customer.get("username", ""),
                "customer_id": customer.get("id", ""),
                "unread_count": convo.get("unread_count", ""),
                "conversation_updated": convo.get("updated_time", ""),
                "message_time": m.get("created_time", ""),
                "sender": sender.get("username", sender.get("id", "")),
                "direction": "outbound" if sender.get("id") == ig_id else "inbound",
                "message": m.get("message", ""),
            })
        if i % 25 == 0:
            print(f"  processed {i}/{len(convos)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    paths = write_csvs(rows_by_staff, stamp)

    print("\nPer-person exports:")
    for person, path in paths.items():
        n_convos = len({r["conversation_id"] for r in rows_by_staff[person]})
        print(f"  {person}: {n_convos} conversations -> {path}")

    if SERVICE_ACCOUNT_JSON:
        print("\nCreating Google Sheets...")
        for person, url in write_sheets(rows_by_staff, stamp).items():
            print(f"  {person}: {url}")


if __name__ == "__main__":
    main()
