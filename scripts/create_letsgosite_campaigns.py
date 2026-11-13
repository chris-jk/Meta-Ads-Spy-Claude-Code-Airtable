#!/usr/bin/env python3
"""Create one Campaigns row per image in the website_design_lead_capture batch.

Free lane  -> https://free.letsgosite.com
$99 lane   -> https://letsgosite.com/99
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_ID = "appGYMvAoZJ0ZciMh"
CLIENT_REC_ID = "recOW45pCqmu8iQK1"  # Lets Go Site
BATCH = Path(
    "/Users/chris/GitHub/tools/meta-ads-spy/letsgosite_ads/campaigns/"
    "website_design_lead_capture/2026-05-19_chatgpt_square_posters"
)

FREE_URL = "https://free.letsgosite.com"
NINETY_NINE_URL = "https://letsgosite.com/99"

# (file_stem, lane, headline, primary_text)
ADS = [
    ("01-ai-generated-website-in-30-seconds",
     "$99",
     "AI-Generated Website in 30 Seconds",
     "Watch your business get online in 30 seconds. Custom website, one-time $99 — no monthly fees. Apply now."),
    ("02-custom-website-design-for-99",
     "$99",
     "Custom Website Design for $99",
     "A real custom website built for your business. Just $99, one-time. No monthly fees, no subscriptions. Apply now."),
    ("03-free-website-design-apply-now",
     "Free",
     "Free Website Design — Apply Now",
     "We're giving away free website designs to a small group of businesses this month. Apply now to see if you qualify."),
    ("04-need-a-website-apply-now",
     "$99",
     "Need a Website? Apply Now",
     "Get a custom website for your business — one-time $99, no monthly fees. Apply now and we'll get you online fast."),
    ("05-get-your-business-online-apply-now",
     "$99",
     "Get Your Business Online",
     "Most customers find you online first. Get a custom website for $99, one-time. Apply now."),
    ("06-custom-website-design-for-99-alt",
     "$99",
     "Custom Website Design — $99",
     "A real custom website built for your business. Just $99, one-time. No monthly fees. Apply now."),
    ("07-free-website-design-apply-now-alt",
     "Free",
     "Free Website Design — Apply Now",
     "Limited free website designs available this month. Apply now to see if your business qualifies."),
    ("08-need-a-website-apply-now-alt",
     "$99",
     "Need a Website? Apply Now",
     "Get your business online with a custom $99 website. One-time fee, no monthly bills. Apply now."),
    ("09-get-your-business-online-apply-now-alt",
     "$99",
     "Get Your Business Online",
     "Your customers are searching online. Get a custom website for one-time $99. Apply now."),
    ("10-website-design-one-time-99",
     "$99",
     "Website Design — One-Time $99",
     "Pay once, $99. Custom website built for your business. No monthly fees, no subscriptions. Apply now."),
    ("11-pay-once-website-design-99",
     "$99",
     "Pay Once — $99, Yours For Life",
     "Stop paying monthly website fees. One-time $99 — the website is yours forever. Apply now."),
    ("12-custom-website-no-monthly-fees-99",
     "$99",
     "$99 Custom Website — No Monthly Fees",
     "A custom website built for your business with zero monthly fees. One-time $99. Apply now."),
    ("13-need-a-website-one-time-99",
     "$99",
     "Need a Website? One-Time $99",
     "Get a custom website for your business — one-time $99, no monthly fees. Apply now."),
]


def main():
    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        sys.exit("AIRTABLE_PAT not set in .env")

    from pyairtable import Api
    api = Api(pat)
    table = api.base(BASE_ID).table("Campaigns")

    # Idempotency: don't recreate rows that already exist by Name
    existing = {r["fields"].get("Name"): r["id"] for r in table.all(fields=["Name"])}

    # Index all PNGs under the batch by stem (files live in angle subfolders)
    pngs_by_stem = {p.stem: p for p in BATCH.rglob("*.png")}

    created = 0
    skipped = 0
    failed = 0
    attachment_failed = 0

    for stem, lane, headline, primary_text in ADS:
        name = f"[{lane}] {stem}"
        if name in existing:
            print(f"  skip (exists): {name}")
            skipped += 1
            continue

        img_path = pngs_by_stem.get(stem)
        if not img_path or not img_path.exists():
            print(f"  MISSING file: {stem}.png under {BATCH}")
            failed += 1
            continue

        dest = FREE_URL if lane == "Free" else NINETY_NINE_URL

        fields = {
            "Name": name,
            "Client": [CLIENT_REC_ID],
            "Status": "Draft",
            "Objective": "OUTCOME_LEADS",
            "Daily Budget (USD)": 10,
            "Countries": ["US"],
            "Age Min": 25,
            "Age Max": 65,
            "Genders": "All",
            "Placements": "Automatic",
            "Headline": headline,
            "Primary Text": primary_text,
            "CTA": "SIGN_UP",
            "Destination URL": dest,
        }

        try:
            rec = table.create(fields)
            print(f"  created: {name}  ({rec['id']})")
            created += 1
        except Exception as e:
            print(f"  FAILED create {name}: {e}")
            failed += 1
            continue

        # Attach image. If upload fails, roll back the row so we don't leave
        # an orphan with no creative (re-runs are idempotent by Name).
        try:
            with open(img_path, "rb") as fh:
                content = fh.read()
            table.upload_attachment(
                record_id=rec["id"],
                field="Creative Image",
                filename=img_path.name,
                content=content,
                content_type="image/png",
            )
            print(f"      attached: {img_path.name} ({len(content)//1024} KB)")
        except Exception as e:
            print(f"      FAILED attach {img_path.name} to {rec['id']}: {e}")
            try:
                table.delete(rec["id"])
                print(f"      rolled back orphan row {rec['id']} ({name})")
                created -= 1
                attachment_failed += 1
            except Exception as del_err:
                print(f"      FAILED to delete orphan {rec['id']} ({name}): {del_err}")
                attachment_failed += 1

    print(f"\n{created} created, {skipped} skipped, {failed} failed, {attachment_failed} attachment failed.")


if __name__ == "__main__":
    main()
