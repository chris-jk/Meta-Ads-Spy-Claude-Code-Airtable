#!/usr/bin/env python3
"""Check the matched/approximate size of one or more Custom Audiences.

Meta matches uploaded customer lists asynchronously. Use this to poll an
audience's size after upload.

Example:
    python3 check_audience_size.py 120246503875260674 120246503875730674
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

BASE_URL = "https://graph.facebook.com/v21.0"

# operation_status codes worth surfacing in plain English
STATUS = {
    200: "ready",
    300: "processing/matching",
    400: "error",
    410: "too few matched users to populate",
}


def main() -> int:
    load_dotenv()
    token = os.getenv("META_MARKETING_TOKEN")
    if not token:
        raise SystemExit("META_MARKETING_TOKEN not set in .env")
    if len(sys.argv) < 2:
        raise SystemExit("usage: check_audience_size.py <audience_id> [<audience_id> ...]")

    fields = "name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,operation_status,delivery_status,time_updated"
    for aud_id in sys.argv[1:]:
        r = requests.get(
            f"{BASE_URL}/{aud_id}",
            params={"fields": fields, "access_token": token},
            timeout=60,
        )
        if r.status_code != 200:
            print(f"{aud_id}: ERROR {r.status_code} {r.text[:200]}")
            continue
        d = r.json()
        lo = d.get("approximate_count_lower_bound")
        hi = d.get("approximate_count_upper_bound")
        op = (d.get("operation_status") or {})
        code = op.get("code")
        size = (
            f"{lo}–{hi}" if lo is not None and hi is not None
            else (str(lo) if lo is not None else "not available yet")
        )
        print(
            f"{d.get('name')} ({d.get('subtype')}) [{aud_id}]\n"
            f"  size: {size}\n"
            f"  status: {STATUS.get(code, op.get('description') or code)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
