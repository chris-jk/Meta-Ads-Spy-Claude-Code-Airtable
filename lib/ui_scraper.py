"""Scrape the Meta Ad Library web UI for a page's ads.

Used when the Ad Library API returns nothing — which happens for all commercial
ads targeted outside the EU/UK (the API only exposes EU/UK commercial ads plus
political ads globally). The UI shows everything; this module extracts the same
fields the API would have returned so the rest of the pipeline is unchanged.

Returns dicts shaped like fetch_ads_for_page output:
  id, page_id, page_name, ad_creative_bodies, ad_creative_link_titles,
  ad_creative_link_descriptions, ad_creative_link_captions, ad_snapshot_url,
  ad_delivery_start_time, ad_delivery_stop_time, publisher_platforms, languages,
  target_ages, target_gender, target_locations
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

from lib.creative_extractor import create_driver

_MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1
)}

_LIB_ID_RE = re.compile(r"Library ID:\s*(\d+)")
_STARTED_RE = re.compile(r"Started running on (\d+) (\w+) (\d{4})")
_ACTIVE_TIME_RE = re.compile(r"Total active time \d")
_LANDING_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{3,}\.[A-Z]{2,}$")  # OFFER.THEFREEWEBSITEGUYS.COM
_VIDEO_TIME_RE = re.compile(r"^\d:\d{2}\s*/\s*\d:\d{2}$|^/$|^0:\d{2}$")

_STRIP_TOKENS = {
    "Sponsored", "Open Drop-down", "See ad details", "See summary details",
    "Platforms", "Apply now", "Learn more", "Sign up", "Shop now", "Get offer",
    "Active", "Inactive", "Low impression count", "Impressions:", "Get quote",
}


def _strip_html(html: str) -> list[str]:
    """Convert a card's HTML chunk to a list of visible text lines."""
    text = re.sub(r"<[^>]+>", "\n", html)
    text = (text
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#039;", "'").replace("&quot;", '"').replace("​", ""))
    lines = [l.strip() for l in text.split("\n")]
    return [l for l in lines if l]


def _parse_date(day: str, mon: str, year: str) -> Optional[str]:
    m = _MONTHS.get(mon[:3])
    if not m:
        return None
    try:
        return datetime(int(year), m, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_card(html_chunk: str, page_id: str) -> Optional[dict]:
    """Parse a single ad card's HTML chunk into an ad dict."""
    lib_m = _LIB_ID_RE.search(html_chunk)
    if not lib_m:
        return None
    lib_id = lib_m.group(1)

    lines = _strip_html(html_chunk)
    if not lines:
        return None

    # Find start date
    start_date = None
    for l in lines:
        sm = _STARTED_RE.match(l)
        if sm:
            start_date = _parse_date(*sm.groups())
            break

    # Find page name + body: page name is the line just before "Sponsored",
    # body is everything between "Sponsored" and the first uppercase domain
    # (the landing caption) — or end of card if no landing.
    page_name = ""
    body_lines: list[str] = []
    landing = ""
    headline = ""
    description = ""

    try:
        sponsored_idx = lines.index("Sponsored")
        # Page name = closest preceding line that isn't a UI token / date / id
        for i in range(sponsored_idx - 1, -1, -1):
            cand = lines[i]
            if (cand in _STRIP_TOKENS
                or _LIB_ID_RE.match(cand)
                or _STARTED_RE.match(cand)
                or _ACTIVE_TIME_RE.search(cand)
                or cand.startswith("Library ID")
                or cand.startswith("Started running")
                or cand.startswith("Total active time")
                or cand.startswith("This ad has")
                or "ads use this creative" in cand
                or cand.startswith("<")
                or cand == "&lt;100"):
                continue
            page_name = cand
            break

        # Walk forward from Sponsored, collecting body until landing or end
        i = sponsored_idx + 1
        while i < len(lines):
            l = lines[i]
            if _VIDEO_TIME_RE.match(l) or l in {"/", "0:00"}:
                i += 1
                continue
            if _LANDING_RE.match(l):
                landing = l
                if i + 1 < len(lines):
                    headline = lines[i + 1]
                if i + 2 < len(lines):
                    nxt = lines[i + 2]
                    if nxt not in _STRIP_TOKENS and not _LIB_ID_RE.match(nxt):
                        description = nxt
                break
            if (l in _STRIP_TOKENS
                or _LIB_ID_RE.match(l)
                or _STARTED_RE.match(l)
                or _ACTIVE_TIME_RE.search(l)):
                i += 1
                continue
            body_lines.append(l)
            i += 1
    except ValueError:
        # No "Sponsored" found — skip card
        return None

    body = "\n".join(body_lines).strip()

    # Active vs inactive — look for "Inactive" keyword; default to Active since
    # we filter by active_status=active in the URL
    is_inactive = any(l == "Inactive" for l in lines)
    stop_date = ""  # UI doesn't expose stop date directly without expanding details

    return {
        "id": lib_id,
        "page_id": page_id,
        "page_name": page_name,
        "ad_creative_bodies": [body] if body else [],
        "ad_creative_link_titles": [headline] if headline else [],
        "ad_creative_link_descriptions": [description] if description else [],
        "ad_creative_link_captions": [landing] if landing else [],
        "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={lib_id}",
        "ad_delivery_start_time": start_date or "",
        "ad_delivery_stop_time": stop_date,
        "publisher_platforms": [],  # UI represents these as icons; skip for now
        "languages": [],
        "target_ages": [],
        "target_gender": "",
        "target_locations": [],
        "eu_total_reach": None,
    }


def _build_url(page_id: str, country: str, active_only: bool) -> str:
    active_status = "active" if active_only else "all"
    return (
        "https://www.facebook.com/ads/library/"
        f"?active_status={active_status}"
        "&ad_type=all"
        f"&country={country}"
        "&is_targeted_country=false"
        "&media_type=all"
        "&search_type=page"
        "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
        f"&view_all_page_id={page_id}"
    )


def _scroll_until_loaded(driver: webdriver.Chrome, max_ads: int, max_scrolls: int = 30) -> int:
    """Scroll the page until we have max_ads library IDs visible, or no new ads load."""
    prev_count = 0
    stagnant = 0
    for i in range(max_scrolls):
        ids = set(_LIB_ID_RE.findall(driver.page_source))
        count = len(ids)
        if count >= max_ads:
            return count
        if count == prev_count:
            stagnant += 1
            if stagnant >= 3:
                return count
        else:
            stagnant = 0
        prev_count = count
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.2)
    return len(set(_LIB_ID_RE.findall(driver.page_source)))


def scrape_page_ads(
    page_id: str,
    country: str = "US",
    max_ads: int = 100,
    active_only: bool = True,
    headless: bool = True,
    driver: Optional[webdriver.Chrome] = None,
) -> list[dict]:
    """Scrape the public Ad Library web UI for a page's ads.

    Returns list of dicts shaped like the Ad Library API's ads_archive response,
    so the rest of pull_ads.py can consume them unchanged.
    """
    own_driver = driver is None
    if own_driver:
        driver = create_driver(headless=headless)

    try:
        url = _build_url(page_id, country, active_only)
        driver.get(url)

        try:
            WebDriverWait(driver, 15).until(
                lambda d: "Library ID" in d.page_source or "No ads match" in d.page_source
            )
        except Exception:
            pass

        if "No ads match" in driver.page_source and "Library ID" not in driver.page_source:
            return []

        n_loaded = _scroll_until_loaded(driver, max_ads)
        if n_loaded == 0:
            return []

        src = driver.page_source

        # Slice into per-card chunks: each card runs from one Library ID match
        # to the next. The last card runs to end-of-page.
        matches = list(_LIB_ID_RE.finditer(src))
        seen: set[str] = set()
        ads: list[dict] = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else min(m.start() + 20000, len(src))
            chunk = src[m.start():end]
            ad = _parse_card(chunk, page_id)
            if not ad or ad["id"] in seen:
                continue
            seen.add(ad["id"])
            ads.append(ad)
            if len(ads) >= max_ads:
                break

        return ads
    finally:
        if own_driver:
            driver.quit()
