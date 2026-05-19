#!/usr/bin/env python3
"""Ingest a folder of creative assets into the Airtable Ad Briefs table.

Folder convention:
    <campaign_root>/
        <batch_date>_<batch_label>/
            <angle_slug>/
                01-some-file.png
                02-some-file.png

One Ad Brief row is created (or upserted) per angle subfolder, with:
    Slug      = angle_slug
    Angle     = angle_slug humanized
    Style     = "A - Typography" (default; override via --style)
    Status    = "Generated" (PNGs exist locally)
    Notes     = path back to local folder

Each PNG inside the subfolder is uploaded as a Candidate attachment.
File uploads are idempotent: existing attachments with matching filenames are skipped.

Usage:
    python3 scripts/ingest_creative_folder.py letsgosite_ads/campaigns/website_design_lead_capture/2026-05-19_chatgpt_square_posters
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_ID = os.getenv("AIRTABLE_BASE_ID")
PAT = os.getenv("AIRTABLE_PAT")
if not (BASE_ID and PAT):
    sys.exit("Missing AIRTABLE_BASE_ID or AIRTABLE_PAT in .env")


def humanize(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("batch_folder", help="Path to a batch folder containing per-angle subfolders")
    p.add_argument("--style", default="A - Typography", help="Default Style for new briefs")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    batch = Path(args.batch_folder).resolve()
    if not batch.is_dir():
        sys.exit(f"Not a directory: {batch}")

    # Find angle subfolders (directories containing image files)
    angle_dirs = []
    for child in sorted(batch.iterdir()):
        if child.is_dir():
            pngs = sorted([f for f in child.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")])
            if pngs:
                angle_dirs.append((child, pngs))

    if not angle_dirs:
        sys.exit(f"No angle subfolders with images found inside {batch}")

    print(f"Found {len(angle_dirs)} angle(s) in {batch.name}:")
    for d, files in angle_dirs:
        print(f"  {d.name}: {len(files)} file(s)")

    if args.dry_run:
        return

    from pyairtable import Api
    api = Api(PAT)
    table = api.base(BASE_ID).table("Ad Briefs")

    # Upsert one row per angle
    repo_root = Path(__file__).resolve().parent.parent
    rel_batch = batch.relative_to(repo_root) if repo_root in batch.parents or batch == repo_root else batch

    records = []
    for angle_dir, _ in angle_dirs:
        slug = angle_dir.name
        records.append({"fields": {
            "Slug": slug,
            "Angle": humanize(slug),
            "Style": args.style,
            "Status": "Generated",
            "Notes": f"Source folder: {rel_batch}/{slug}/",
        }})

    print(f"\nUpserting {len(records)} Ad Brief row(s)...")
    result = table.batch_upsert(records, key_fields=["Slug"])
    created = len(result.get("createdRecords", []))
    updated = len(result.get("updatedRecords", []))
    print(f"  Created: {created}, Updated: {updated}")

    # Re-fetch rows to get record IDs and existing attachment filenames (for idempotent uploads)
    rows = table.all(fields=["Slug", "Candidates"])
    by_slug = {}
    for r in rows:
        f = r["fields"]
        slug = f.get("Slug")
        if not slug:
            continue
        existing_filenames = {a["filename"] for a in f.get("Candidates", [])}
        by_slug[slug] = (r["id"], existing_filenames)

    # Upload PNGs (skip ones already attached)
    print("\nUploading candidates...")
    for angle_dir, files in angle_dirs:
        slug = angle_dir.name
        entry = by_slug.get(slug)
        if not entry:
            print(f"  {slug}: row not found (unexpected)")
            continue
        rec_id, existing = entry
        for path in files:
            if path.name in existing:
                print(f"  {slug}/{path.name}: already attached, skipping")
                continue
            with open(path, "rb") as fh:
                content = fh.read()
            try:
                table.upload_attachment(
                    record_id=rec_id,
                    field="Candidates",
                    filename=path.name,
                    content=content,
                    content_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                )
                print(f"  {slug}/{path.name}: uploaded ({len(content)//1024} KB)")
            except Exception as e:
                print(f"  {slug}/{path.name}: FAILED ({e})")

    print("\nDone.")


if __name__ == "__main__":
    main()
