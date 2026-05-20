#!/usr/bin/env python3
"""Launch campaigns from the Airtable 'Campaigns' table to the Meta Marketing API.

Reads rows where Status='Ready to launch', creates campaign + ad set + ad
(PAUSED by default), and writes the resulting IDs back to the row.

Usage:
    python3 create_campaigns.py --base-id appXXXX                 # all ready rows
    python3 create_campaigns.py --base-id appXXXX --record-id recX
    python3 create_campaigns.py --base-id appXXXX --client "Lets Go Site"
    python3 create_campaigns.py --base-id appXXXX --dry-run
    python3 create_campaigns.py --base-id appXXXX --status ACTIVE  # go live
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from lib.marketing_api import (
    MarketingAPI,
    MarketingAPIError,
    download,
    verify_marketing_token,
)

load_dotenv()

CLIENTS_TABLE = "Clients"
CAMPAIGNS_TABLE = "Campaigns"


def load_clients(base):
    table = base.table(CLIENTS_TABLE)
    return {r["id"]: r["fields"] for r in table.all()}


def get_ready_campaigns(base, record_id=None, client_name=None):
    table = base.table(CAMPAIGNS_TABLE)
    if record_id:
        return [table.get(record_id)]
    rows = table.all(formula="{Status} = 'Ready to launch'")
    if client_name:
        # Filter by linked client name (we have to look it up via Clients table)
        clients_table = base.table(CLIENTS_TABLE)
        matching = [
            c["id"] for c in clients_table.all()
            if c["fields"].get("Client Name") == client_name
        ]
        rows = [
            r for r in rows
            if any(cid in matching for cid in r["fields"].get("Client", []))
        ]
    return rows


def resolve_interests(api: MarketingAPI, names_csv: str | None) -> list[str]:
    if not names_csv:
        return []
    ids = []
    for raw in names_csv.split(","):
        name = raw.strip()
        if not name:
            continue
        hits = api.search_interests(name, limit=1)
        if hits:
            ids.append(hits[0]["id"])
        else:
            print(f"  ! Interest '{name}' not found — skipped")
    return ids


def launch_one(row, clients, token, target_status, dry_run):
    # Partial-failure note: this creates Campaign -> Ad Set -> Creative -> Ad
    # in sequence on the Meta side. If a later step fails, earlier objects are
    # NOT torn down — they remain in the ad account (PAUSED by default) and the
    # Airtable row is marked Failed with the error in Launch Log. Clean up the
    # orphan objects manually in Ads Manager before re-running, or the next run
    # will create duplicates.
    fields = row["fields"]
    name = fields.get("Name") or "(unnamed)"
    log_lines = []

    def log(msg):
        print(f"  {msg}")
        log_lines.append(msg)

    # Resolve client
    client_links = fields.get("Client") or []
    if not client_links:
        raise ValueError("No Client linked")
    client = clients.get(client_links[0])
    if not client:
        raise ValueError(f"Client {client_links[0]} not found in Clients table")

    ad_account = client.get("Ad Account ID")
    page_id = client.get("Page ID")
    if not ad_account or not page_id:
        raise ValueError(f"Client '{client.get('Client Name')}' missing Ad Account ID or Page ID")

    api = MarketingAPI(
        ad_account_id=ad_account,
        page_id=page_id,
        access_token=token,
        instagram_account_id=client.get("Instagram Account ID") or None,
    )

    # Validate required campaign fields
    required = ["Objective", "Daily Budget (USD)", "Countries", "Headline", "Primary Text", "CTA", "Destination URL"]
    missing = [r for r in required if not fields.get(r)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    image_attachments = fields.get("Creative Image") or []
    video_url = fields.get("Video URL")
    if not image_attachments and not video_url:
        raise ValueError("Need either Creative Image or Video URL")

    objective = fields["Objective"]
    countries = fields["Countries"]
    age_min = int(fields.get("Age Min") or 18)
    age_max = int(fields.get("Age Max") or 65)
    genders = fields.get("Genders") or "All"
    placements = fields.get("Placements") or "Automatic"
    start_date = fields.get("Start Date")
    end_date = fields.get("End Date")

    start_time = f"{start_date}T00:00:00+0000" if start_date else None
    end_time = f"{end_date}T23:59:59+0000" if end_date else None

    pixel_id = client.get("Pixel ID") or None
    pixel_event = fields.get("Pixel Event") or None
    if objective in ("OUTCOME_SALES", "OUTCOME_LEADS"):
        if not pixel_id:
            raise ValueError(f"{objective} requires Pixel ID on client '{client.get('Client Name')}'")
        if not pixel_event:
            raise ValueError(f"{objective} requires 'Pixel Event' on this campaign row")

    if dry_run:
        log(f"[DRY RUN] Would create campaign '{name}' in {ad_account}")
        log(f"  Objective: {objective}, Budget: ${fields['Daily Budget (USD)']}/day")
        log(f"  Targeting: {countries}, ages {age_min}-{age_max}, {genders}, {placements}")
        log(f"  Creative: {'image' if image_attachments else 'video'} -> {fields['Destination URL']}")
        if pixel_id and pixel_event:
            log(f"  Pixel: {pixel_id} optimizing for {pixel_event}")
        return {"dry_run": True, "log": "\n".join(log_lines)}

    # 1. Campaign
    log(f"Creating campaign '{name}'...")
    camp = api.create_campaign(name=name, objective=objective, status=target_status)
    campaign_id = camp["id"]
    log(f"  Campaign ID: {campaign_id}")

    # 2. Resolve interests
    interest_ids = resolve_interests(api, fields.get("Interests"))
    if interest_ids:
        log(f"  Resolved {len(interest_ids)} interests")

    # 3. Ad Set
    log("Creating ad set...")
    adset = api.create_adset(
        campaign_id=campaign_id,
        name=f"{name} - Ad Set",
        daily_budget_usd=float(fields["Daily Budget (USD)"]),
        countries=countries,
        age_min=age_min,
        age_max=age_max,
        genders=genders,
        placements=placements,
        objective=objective,
        start_time=start_time,
        end_time=end_time,
        status=target_status,
        interest_ids=interest_ids,
        pixel_id=pixel_id,
        custom_event_type=pixel_event,
    )
    adset_id = adset["id"]
    log(f"  Ad Set ID: {adset_id}")

    # 4. Upload creative
    log("Uploading creative...")
    if image_attachments:
        att = image_attachments[0]
        img_bytes, fn = download(att["url"])
        image_hash = api.upload_image(img_bytes, filename=fn)
        log(f"  Image hash: {image_hash}")
        creative = api.create_image_creative(
            name=f"{name} - Creative",
            image_hash=image_hash,
            headline=fields["Headline"],
            primary_text=fields["Primary Text"],
            description=fields.get("Description") or "",
            cta=fields["CTA"],
            link_url=fields["Destination URL"],
        )
    else:
        vid_bytes, fn = download(video_url)
        video_id = api.upload_video(vid_bytes, filename=fn)
        log(f"  Video ID: {video_id} (processed)")
        # Thumbnail: grab from page profile pic as fallback, or require user-supplied
        # Simpler: pull first frame would need ffmpeg; for now reuse video URL as thumb
        # via Meta's auto-thumbnail. Pass empty image_hash — Meta will use video thumbnail.
        creative = api.create_video_creative(
            name=f"{name} - Creative",
            video_id=video_id,
            image_hash="",
            headline=fields["Headline"],
            primary_text=fields["Primary Text"],
            description=fields.get("Description") or "",
            cta=fields["CTA"],
            link_url=fields["Destination URL"],
        )
    creative_id = creative["id"]
    log(f"  Creative ID: {creative_id}")

    # 5. Ad
    log("Creating ad...")
    ad = api.create_ad(
        adset_id=adset_id,
        creative_id=creative_id,
        name=f"{name} - Ad",
        status=target_status,
    )
    ad_id = ad["id"]
    log(f"  Ad ID: {ad_id}")

    return {
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "ad_id": ad_id,
        "log": "\n".join(log_lines),
    }


def main():
    parser = argparse.ArgumentParser(description="Launch Meta ad campaigns from Airtable")
    parser.add_argument("--base-id", required=True)
    parser.add_argument("--record-id", help="Launch a single campaign by Airtable record ID")
    parser.add_argument("--client", help="Filter to campaigns for one client")
    parser.add_argument("--dry-run", action="store_true", help="Validate + print, no API writes")
    parser.add_argument(
        "--status",
        default="PAUSED",
        choices=["PAUSED", "ACTIVE"],
        help="Campaign/ad set/ad status after creation (default: PAUSED)",
    )
    args = parser.parse_args()

    pat = os.getenv("AIRTABLE_PAT")
    token = os.getenv("META_MARKETING_TOKEN")
    if not pat:
        sys.exit("AIRTABLE_PAT not set in .env")
    if not token and not args.dry_run:
        sys.exit("META_MARKETING_TOKEN not set in .env (System User token with ads_management)")

    from pyairtable import Api
    api_at = Api(pat)
    base = api_at.base(args.base_id)

    clients = load_clients(base)
    rows = get_ready_campaigns(base, record_id=args.record_id, client_name=args.client)

    if not rows:
        print("No campaigns to launch (none with Status='Ready to launch').")
        return

    print(f"Found {len(rows)} campaign(s) to launch (status target: {args.status}, dry_run: {args.dry_run})")

    campaigns_table = base.table(CAMPAIGNS_TABLE)
    success = failed = 0

    for row in rows:
        rid = row["id"]
        name = row["fields"].get("Name") or "(unnamed)"
        print(f"\n--- {name} ({rid}) ---")
        try:
            result = launch_one(row, clients, token, args.status, args.dry_run)
            if args.dry_run:
                campaigns_table.update(rid, {"Launch Log": result["log"]})
            else:
                campaigns_table.update(rid, {
                    "Campaign ID": result["campaign_id"],
                    "Ad Set ID": result["adset_id"],
                    "Ad ID": result["ad_id"],
                    "Status": "Launched",
                    "Launched At": datetime.now(timezone.utc).isoformat(),
                    "Launch Log": result["log"],
                })
            success += 1
        except (MarketingAPIError, ValueError, Exception) as e:
            err = f"FAILED: {e}"
            print(f"  {err}")
            try:
                campaigns_table.update(rid, {
                    "Status": "Failed",
                    "Launch Log": err,
                })
            except Exception as e2:
                print(f"  (also failed to write log back to Airtable: {e2})")
            failed += 1

    print(f"\n{success} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
