#!/usr/bin/env python3
"""Build a Meta Custom Audience from Grow Guide's confirmed app members.

Pulls emails straight from Supabase Auth, SHA-256 hashes them locally, and
uploads only the digests. Raw addresses never touch disk, stdout, or a CSV —
which is the point: the existing lookalike seeds are stale April-2025 exports,
and re-exporting to a file just creates another one to leak later.

Reads Supabase credentials from the Grow Guide app's .env (it owns them) and
META_MARKETING_TOKEN from this repo's .env. The ad account comes from
--ad-account, or from `ad_account` in ads_config.json (git-ignored; copy
ads_config.example.json), so the account number never sits in this file.

Examples
--------
    # count only, no API writes, nothing leaves the machine:
    python3.11 build_app_member_audience.py --dry-run

    # create the seed audience and a 1% US lookalike:
    python3.11 build_app_member_audience.py \
        --ad-account act_<id> \
        --name "GG App Members (confirmed)" \
        --lookalike

CONSENT: USER_PROVIDED_ONLY certifies these people gave you their address
directly. App signups qualify. Scraped or purchased lists do not.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

import requests
from dotenv import load_dotenv

from lib import ads_config
from lib.marketing_api import MarketingAPI, verify_marketing_token

BATCH_SIZE = 10_000  # Meta's per-call ceiling for /users
# Supabase's admin list 500s ("Database error finding users") above ~200 on this
# project. 200 is the largest page size that returns cleanly.
PER_PAGE = 200
MIN_SLICE = 25  # give up on a range this small and skip it
MAX_PAGES = 500  # guard against an endless loop if the API stops signalling the end
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Supabase credentials live in the Grow Guide repos, not this one; this repo
# only has the ad token. The service-role key has moved between these files and
# key names over time, and the app repo's slot is currently blank — so try each
# candidate and keep the first that actually clears the admin endpoint.
CRED_FILES = [
    os.path.expanduser("~/GitHub/growguide-web/.dev.vars"),
    os.path.expanduser("~/GitHub/grow_guide_app/.env"),
]
SERVICE_KEY_NAMES = [
    "GROWGUIDE_SUPABASE_SERVICE_ROLE_KEY",
    "GROWGUIDE_SUPABASE_SERVICE_KEY",
    "GROWGUIDE_SUPABASE_KEY",
]


def resolve_supabase_creds() -> tuple[str, str]:
    """Return (url, service_key) for the first key with admin access.

    The anon key returns 403 not_admin here, so a live probe is the only
    reliable way to tell the two apart — the names don't distinguish them.
    """
    from dotenv import dotenv_values

    for path in CRED_FILES:
        if not os.path.exists(path):
            continue
        vals = dotenv_values(path)
        url = (vals.get("GROWGUIDE_SUPABASE_URL") or "").rstrip("/")
        if not url:
            continue
        for name in SERVICE_KEY_NAMES:
            key = vals.get(name)
            if not key:
                continue
            try:
                probe = requests.get(
                    f"{url}/auth/v1/admin/users",
                    params={"page": 1, "per_page": 1},
                    headers={"apikey": key, "Authorization": f"Bearer {key}"},
                    timeout=30,
                )
            except requests.RequestException:
                continue
            if probe.status_code == 200:
                print(f"Using {name} from {os.path.basename(path)}")
                return url, key

    raise SystemExit(
        "No Supabase key with admin access found. Checked "
        + ", ".join(os.path.basename(p) for p in CRED_FILES)
    )


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fetch_members(url: str, service_key: str, confirmed_only: bool) -> tuple[list[str], dict]:
    """Page through Supabase Auth and return normalized emails + drop counts.

    Returns hashes-ready addresses (lowercased, trimmed) and a stats dict. The
    caller hashes; this function is the only place raw addresses exist.
    """
    session = requests.Session()
    session.headers.update({"apikey": service_key, "Authorization": f"Bearer {service_key}"})

    emails: list[str] = []
    stats = {"seen": 0, "unconfirmed": 0, "malformed": 0, "duplicate": 0, "unreadable": 0}
    seen: set[str] = set()
    page = 1

    def get_slice(offset: int, size: int) -> list[dict] | None:
        """Fetch `size` users from `offset`, or None if the API 500s or the
        request fails in transport.

        Uses page/per_page as an offset pair: with per_page == size, page N
        covers offset (N-1)*size. Requires offset to be a multiple of size.
        """
        try:
            resp = session.get(
                f"{url.rstrip('/')}/auth/v1/admin/users",
                params={"page": offset // size + 1, "per_page": size},
                timeout=60,
            )
        except requests.RequestException:
            return None
        if resp.status_code == 200:
            return resp.json().get("users", [])
        return None

    def fetch_resilient(offset: int, size: int) -> list[dict]:
        """Fetch a slice, halving on failure to isolate an unreadable record.

        At least one row in this project's auth.users makes Supabase's admin
        list 500 ("Database error finding users"). Splitting the failing range
        quarantines it so a single bad record costs a handful of addresses
        instead of the whole page.
        """
        users = get_slice(offset, size)
        if users is not None:
            return users
        if size <= MIN_SLICE:
            stats["unreadable"] += size
            print(f"  ! skipping {size} unreadable user(s) at offset {offset}")
            return []
        half = size // 2
        return fetch_resilient(offset, half) + fetch_resilient(offset + half, half)

    while page <= MAX_PAGES:
        users = fetch_resilient((page - 1) * PER_PAGE, PER_PAGE)
        # An empty slice means we're past the last user. Quarantined records
        # make len(users) < PER_PAGE unreliable as an end-of-list signal, so a
        # fully empty page is the only terminator we can trust.
        if not users:
            break

        for u in users:
            stats["seen"] += 1
            raw = (u.get("email") or "").strip().lower()
            if confirmed_only and not u.get("email_confirmed_at"):
                stats["unconfirmed"] += 1
                continue
            if not EMAIL_RE.match(raw):
                stats["malformed"] += 1
                continue
            if raw in seen:
                stats["duplicate"] += 1
                continue
            seen.add(raw)
            emails.append(raw)

        print(f"  ...page {page}: {stats['seen']} users scanned, {len(emails)} usable so far")
        page += 1

    return emails, stats


def main() -> int:
    load_dotenv()  # this repo — META_MARKETING_TOKEN

    p = argparse.ArgumentParser(description="Build a Meta Custom Audience from app members")
    p.add_argument("--ad-account", default=ads_config.get("ad_account"),
                   help="act_<id> (default: ad_account in ads_config.json)")
    p.add_argument("--name", default="GG App Members (confirmed)")
    p.add_argument("--description", default="Confirmed Grow Guide app signups, uploaded from Supabase Auth")
    p.add_argument("--all-users", action="store_true",
                   help="include unconfirmed signups (default: confirmed only)")
    p.add_argument("--lookalike", action="store_true", help="also create a 1%% Lookalike")
    p.add_argument("--lookalike-country", default="US")
    p.add_argument("--dry-run", action="store_true", help="count + hash only; no API writes")
    args = p.parse_args()

    sb_url, sb_key = resolve_supabase_creds()

    token = os.getenv("META_MARKETING_TOKEN")
    if not token and not args.dry_run:
        raise SystemExit("META_MARKETING_TOKEN not set in .env")
    if not args.ad_account and not args.dry_run:
        raise SystemExit("No ad account: pass --ad-account act_<id> or set ad_account "
                         "in ads_config.json (see ads_config.example.json)")

    print(f"Fetching {'all' if args.all_users else 'confirmed'} members from Supabase Auth...")
    emails, stats = fetch_members(sb_url, sb_key, confirmed_only=not args.all_users)
    hashes = [sha256(e) for e in emails]

    # Counts only. A SHA-256 of an email is still a stable identifier for that
    # person, so neither the address nor its digest is ever printed.
    print(f"\n{stats['seen']} users scanned -> {len(emails)} unique usable emails")
    print(f"  skipped: {stats['unconfirmed']} unconfirmed, "
          f"{stats['malformed']} malformed, {stats['duplicate']} duplicate")

    if len(emails) < 1_000:
        print(f"  ! WARNING: Meta's lookalike seed floor is ~1,000 matched users. "
              f"You have {len(emails)} before matching, and match rates run 40-70%.")

    if args.dry_run:
        batches = (len(hashes) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n[DRY RUN] Would upload {len(hashes)} hashed emails in {batches} batch(es).")
        print("[DRY RUN] No audience created, nothing sent to Meta.")
        return 0

    api = MarketingAPI(ad_account_id=args.ad_account, page_id="", access_token=token)

    print(f"\nVerifying token against {args.ad_account}...")
    acct = verify_marketing_token(token, args.ad_account)
    print(f"  OK: {acct.get('name')} (status {acct.get('account_status')})")

    print(f"Creating Custom Audience '{args.name}'...")
    aud = api.create_custom_audience(args.name, args.description)
    audience_id = aud["id"]
    print(f"  Audience ID: {audience_id}")

    total = 0
    for i in range(0, len(hashes), BATCH_SIZE):
        batch = hashes[i:i + BATCH_SIZE]
        resp = api.add_users_to_audience(audience_id, "EMAIL", batch)
        total += resp.get("num_received", len(batch))
        print(f"  Uploaded batch {i // BATCH_SIZE + 1}: {len(batch)} emails "
              f"(received={resp.get('num_received')}, invalid={resp.get('num_invalid_entries')})")

    print(f"Done. Uploaded {total} hashed emails to {audience_id}.")
    print("Matching runs async; check the size in Ads Manager in ~30-60 min.")

    if args.lookalike:
        print(f"Creating 1% {args.lookalike_country} Lookalike...")
        lal = api.create_lookalike_audience(
            f"{args.name} - Lookalike 1%", audience_id, country=args.lookalike_country
        )
        print(f"  Lookalike ID: {lal['id']} (populates once the seed finishes matching)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
