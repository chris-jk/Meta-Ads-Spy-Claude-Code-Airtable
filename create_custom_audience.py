#!/usr/bin/env python3
"""Build a Meta Custom Audience from a list of phone numbers.

Reads phone numbers from a file (one per line, or a CSV with a `phone`
column), normalizes them to digits with a country code, SHA-256 hashes
them (as Meta requires), creates a customer-list Custom Audience, and
uploads the hashed numbers. Optionally seeds a Lookalike from it.

Examples
--------
    # dump the leads' phones first (from the letsgosite repo):
    #   cd /Users/chris/GitHub/web-apps/letsgosite/free-worker
    #   npx wrangler d1 execute letsgosite --remote --json \
    #     --command "SELECT phone FROM leads WHERE phone IS NOT NULL AND phone<>''" \
    #     | python3 -c "import sys,json; [print(r['phone']) for r in json.load(sys.stdin)[0]['results']]" \
    #     > phones.txt

    python3 create_custom_audience.py \
        --ad-account act_1234567890 \
        --name "Phoenix service businesses" \
        --input phones.txt

    # dry run (normalize + hash only, no API writes):
    python3 create_custom_audience.py --ad-account act_1 --input phones.txt --dry-run

CONSENT: USER_PROVIDED_ONLY certifies you collected these numbers directly
from the people, with consent to use them for advertising. You are asserting
that when you upload. Scraped third-party lists violate Meta's terms.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys

from dotenv import load_dotenv

from lib.marketing_api import MarketingAPI, MarketingAPIError, verify_marketing_token

BATCH_SIZE = 10_000  # Meta's per-call ceiling for /users


def read_phones(path: str) -> list[str]:
    """Read phone strings from a .csv (phone column) or plain text file."""
    if path.lower().endswith(".csv"):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            col = next((c for c in (reader.fieldnames or []) if c.lower() == "phone"), None)
            if not col:
                raise SystemExit(f"No 'phone' column in {path}; columns: {reader.fieldnames}")
            return [row[col] for row in reader if row.get(col)]
    with open(path) as f:
        return [line.strip().strip('"') for line in f if line.strip()]


def normalize(raw: str, default_cc: str = "1") -> str | None:
    """Return digits-with-country-code, or None if it can't be a real number.

    Meta wants digits only, including country code, no symbols. US numbers
    arrive as 10 digits -> prepend '1'. 11-digit already-prefixed pass through.
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return default_cc + digits
    if len(digits) == 11 and digits.startswith(default_cc):
        return digits
    if 11 <= len(digits) <= 15:  # already has some country code
        return digits
    return None  # too short/long to be a phone


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Create a Meta Custom Audience from phone numbers")
    p.add_argument("--ad-account", required=True, help="act_<id>")
    p.add_argument("--input", required=True, help="phones .txt (one per line) or .csv with a phone column")
    p.add_argument("--name", default="Phone match audience")
    p.add_argument("--description", default="Uploaded via create_custom_audience.py")
    p.add_argument("--country", default="1", help="default country calling code for bare 10-digit numbers (default 1=US)")
    p.add_argument("--lookalike", action="store_true", help="also create a 1%% Lookalike from the audience")
    p.add_argument("--lookalike-country", default="US")
    p.add_argument("--dry-run", action="store_true", help="normalize + hash only; no API writes")
    args = p.parse_args()

    token = os.getenv("META_MARKETING_TOKEN")
    if not token and not args.dry_run:
        raise SystemExit("META_MARKETING_TOKEN not set in .env")

    raw = read_phones(args.input)
    normed, dropped = [], []
    seen = set()
    for r in raw:
        n = normalize(r, args.country)
        if n is None:
            dropped.append(r)
        elif n not in seen:
            seen.add(n)
            normed.append(n)
    hashes = [sha256(n) for n in normed]

    # Counts only — never echo phone numbers or their hashes; both identify a
    # person, and a SHA-256 of a phone number is trivially reversible.
    print(f"Read {len(raw)} numbers -> {len(normed)} unique valid, {len(dropped)} dropped, "
          f"{len(raw) - len(dropped) - len(normed)} duplicate")
    if len(normed) < 100:
        print(f"  ! WARNING: Meta needs ~100 MATCHED users to activate an audience; "
              f"you have {len(normed)} total before matching. Expect this to fall short.")

    if args.dry_run:
        print(f"[DRY RUN] Would upload {len(hashes)} hashed numbers in "
              f"{(len(hashes) + BATCH_SIZE - 1) // BATCH_SIZE} batch(es).")
        print("[DRY RUN] No audience created.")
        return 0

    api = MarketingAPI(ad_account_id=args.ad_account, page_id="", access_token=token)

    print(f"Verifying token against {args.ad_account}...")
    acct = verify_marketing_token(token, args.ad_account)
    print(f"  OK: {acct.get('name')} (status {acct.get('account_status')})")

    print(f"Creating Custom Audience '{args.name}'...")
    aud = api.create_custom_audience(args.name, args.description)
    audience_id = aud["id"]
    print(f"  Audience ID: {audience_id}")

    total = 0
    for i in range(0, len(hashes), BATCH_SIZE):
        batch = hashes[i:i + BATCH_SIZE]
        resp = api.add_users_to_audience(audience_id, "PHONE", batch)
        total += resp.get("num_received", len(batch))
        print(f"  Uploaded batch {i // BATCH_SIZE + 1}: {len(batch)} numbers "
              f"(received={resp.get('num_received')}, invalid={resp.get('num_invalid_entries')})")
    print(f"Done. Uploaded {total} hashed phone numbers to {audience_id}.")
    print("Matching runs async on Meta's side; check the audience size in Ads Manager in ~30-60 min.")

    if args.lookalike:
        print("Creating 1% Lookalike...")
        lal = api.create_lookalike_audience(
            f"{args.name} - Lookalike 1%", audience_id, country=args.lookalike_country
        )
        print(f"  Lookalike ID: {lal['id']} (populates only once the seed audience matches enough users)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MarketingAPIError as e:
        print(f"\nMeta API error:\n{e}", file=sys.stderr)
        sys.exit(1)
