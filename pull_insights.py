#!/usr/bin/env python3
"""Pull daily Meta Insights for launched ads and upsert into Airtable Performance table.

Reads launched campaigns from the Campaigns table (those with an Ad ID), calls
Meta's Insights API with time_increment=1 (daily breakdown), and upserts one row
per ad per day into the Performance table keyed on Record Key = "{ad_id}_{YYYY-MM-DD}".

Usage:
    python3 pull_insights.py                       # last 7 days, all launched ads
    python3 pull_insights.py --days 30
    python3 pull_insights.py --since 2026-05-01 --until 2026-05-19
    python3 pull_insights.py --ad-id 1234567890    # single ad
    python3 pull_insights.py --dry-run             # print, don't write
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH = "https://graph.facebook.com/v21.0"

# Map Campaigns.Objective -> the action_type we count as "Results"
RESULT_ACTION_BY_OBJECTIVE = {
    "OUTCOME_LEADS": "lead",
    "OUTCOME_SALES": "offsite_conversion.fb_pixel_purchase",
    "OUTCOME_TRAFFIC": "link_click",
    "OUTCOME_ENGAGEMENT": "post_engagement",
    "OUTCOME_APP_PROMOTION": "mobile_app_install",
    # OUTCOME_AWARENESS has no countable result event — leave blank
}

INSIGHT_FIELDS = ",".join([
    "ad_id", "ad_name", "campaign_id",
    "spend", "impressions", "reach", "frequency",
    "clicks", "ctr", "cpc", "cpm",
    "actions", "action_values",
    "date_start", "date_stop",
])


def get_env(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"{name} not set in .env")
    return v


def find_action(actions, action_type):
    if not actions:
        return 0.0
    for a in actions:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value", 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def fetch_insights(ad_id, since, until, token):
    """Page through daily insights for one ad. Returns list of day dicts."""
    url = f"{GRAPH}/{ad_id}/insights"
    params = {
        "fields": INSIGHT_FIELDS,
        "time_increment": 1,
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "access_token": token,
        "limit": 100,
    }
    rows = []
    while True:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", 30))
            print(f"  rate-limited, sleeping {retry}s")
            time.sleep(retry)
            continue
        if not r.ok:
            print(f"  Meta API error for ad {ad_id}: {r.status_code} {r.text[:200]}")
            return []
        body = r.json()
        rows.extend(body.get("data", []))
        next_url = body.get("paging", {}).get("next")
        if not next_url:
            break
        url = next_url
        params = None  # next_url has params baked in
    return rows


def row_to_airtable(insight, objective, campaign_record_id):
    """Convert one Meta insights day dict to Airtable fields."""
    ad_id = insight.get("ad_id", "")
    day = insight.get("date_start", "")
    record_key = f"{ad_id}_{day}"

    spend = float(insight.get("spend") or 0)
    result_action = RESULT_ACTION_BY_OBJECTIVE.get(objective)
    results = find_action(insight.get("actions"), result_action) if result_action else 0
    cost_per_result = (spend / results) if results else 0

    # ROAS: revenue from purchases / spend
    revenue = find_action(
        insight.get("action_values"),
        "offsite_conversion.fb_pixel_purchase",
    )
    if not revenue:
        revenue = find_action(insight.get("action_values"), "purchase")
    roas = (revenue / spend) if spend else 0

    fields = {
        "Record Key": record_key,
        "Date": day,
        "Ad ID": ad_id,
        "Ad Name": insight.get("ad_name", ""),
        "Spend": spend,
        "Impressions": int(float(insight.get("impressions") or 0)),
        "Reach": int(float(insight.get("reach") or 0)),
        "Frequency": float(insight.get("frequency") or 0),
        "Clicks": int(float(insight.get("clicks") or 0)),
        "CTR": float(insight.get("ctr") or 0) / 100,  # Meta returns %, we store decimal
        "CPC": float(insight.get("cpc") or 0),
        "CPM": float(insight.get("cpm") or 0),
        "Results": int(results),
        "Cost per Result": round(cost_per_result, 2),
        "Conversion Value": round(revenue, 2),
        "ROAS": round(roas, 2),
        "Synced At": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if campaign_record_id:
        fields["Campaign"] = [campaign_record_id]
    return fields


def main():
    p = argparse.ArgumentParser(description="Pull Meta Insights into Airtable Performance table")
    p.add_argument("--days", type=int, default=7, help="Days back from today (default 7). Ignored if --since/--until set.")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--ad-id", help="Pull just one ad ID (skip Campaigns table lookup)")
    p.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.base_id:
        sys.exit("No base ID — pass --base-id or set AIRTABLE_BASE_ID in .env")

    pat = get_env("AIRTABLE_PAT")
    token = get_env("META_MARKETING_TOKEN")

    if args.since and args.until:
        since, until = args.since, args.until
    else:
        today = date.today()
        until = today.isoformat()
        since = (today - timedelta(days=args.days)).isoformat()
    print(f"Range: {since} → {until}")

    from pyairtable import Api
    api = Api(pat)
    base = api.base(args.base_id)
    campaigns_tbl = base.table("Campaigns")
    perf_tbl = base.table("Performance")

    # Build list of (ad_id, objective, campaign_record_id) to fetch
    targets = []
    if args.ad_id:
        targets.append((args.ad_id, None, None))
    else:
        rows = campaigns_tbl.all(fields=["Ad ID", "Objective", "Name"])
        for rec in rows:
            f = rec.get("fields", {})
            ad_id = f.get("Ad ID")
            if ad_id:
                targets.append((ad_id, f.get("Objective"), rec["id"]))
        print(f"Found {len(targets)} launched ad(s) in Campaigns")

    if not targets:
        print("Nothing to pull. Launch a campaign first (Status='Ready to launch' -> create_campaigns.py).")
        return

    all_fields = []
    for ad_id, objective, campaign_rec_id in targets:
        print(f"Fetching ad {ad_id} (objective={objective})...")
        days = fetch_insights(ad_id, since, until, token)
        print(f"  {len(days)} day(s)")
        for d in days:
            all_fields.append(row_to_airtable(d, objective, campaign_rec_id))

    if not all_fields:
        print("No insight rows returned (ads may not have run in this range).")
        return

    if args.dry_run:
        print(f"\nDRY RUN — {len(all_fields)} rows would be upserted:")
        for f in all_fields[:5]:
            print(f"  {f['Record Key']}: spend=${f['Spend']:.2f} results={f['Results']} roas={f['ROAS']}")
        if len(all_fields) > 5:
            print(f"  ... and {len(all_fields) - 5} more")
        return

    print(f"\nUpserting {len(all_fields)} row(s) into Performance...")
    records = [{"fields": f} for f in all_fields]
    result = perf_tbl.batch_upsert(records, key_fields=["Record Key"])
    created = len(result.get("createdRecords", []))
    updated = len(result.get("updatedRecords", []))
    print(f"Done. Created: {created}, Updated: {updated}")


if __name__ == "__main__":
    main()
