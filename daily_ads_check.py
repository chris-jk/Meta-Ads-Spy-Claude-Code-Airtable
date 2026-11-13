#!/usr/bin/env python3
"""Daily health check for the live Grow Guide ad campaigns.

Run by launchd at 15:00 Asia/Ho_Chi_Minh (08:00 UTC), which is after the
Pacific ad day has closed — so "yesterday" is a complete day, not a partial one.

Writes a short report to logs/ads_daily/ and raises a macOS notification when
anything needs attention. Everything it flags is a failure mode we have already
hit once: a campaign that silently stopped delivering, an ad set whose end date
quietly expired, a disapproved ad, or the conversion event going quiet.
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import ads_config

REPO = Path(__file__).resolve().parent
LOG_DIR = REPO / "logs" / "ads_daily"
GRAPH = "https://graph.facebook.com/v21.0"

# Account-specific ids come from ads_config.json (git-ignored), never from this
# file: copy ads_config.example.json to ads_config.json and fill it in.
_IDS = ads_config.load(required=("ad_account", "conversion_id", "dataset_id", "campaigns"))
ACCOUNT = _IDS["ad_account"]            # act_<id>
CONVERSION_ID = _IDS["conversion_id"]   # custom conversion the campaigns optimise for
DATASET_ID = _IDS["dataset_id"]         # dataset (pixel) that receives AppStoreClick
CAMPAIGNS = _IDS["campaigns"]           # {campaign id: label}
# Alert thresholds, as a fraction of the ad set's daily budget.
UNDER_PACING = 0.5
OVER_PACING = 1.3
# An end date closer than this is worth knowing about before it stops delivery.
END_DATE_WARN_DAYS = 14
# The conversion feeds optimisation; if it goes quiet the campaign is flying blind.
CONVERSION_QUIET_HOURS = 48


def load_token():
    env_path = REPO / ".env"
    if not env_path.exists():
        sys.exit("no .env found — cannot authenticate")
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() in ("META_MARKETING_TOKEN", "META_ACCESS_TOKEN"):
                os.environ.setdefault(k.strip(), v.strip())
    tok = os.environ.get("META_MARKETING_TOKEN") or os.environ.get("META_ACCESS_TOKEN")
    if not tok:
        sys.exit("no META_MARKETING_TOKEN / META_ACCESS_TOKEN in .env")
    return tok


def get(token, path, **params):
    params["access_token"] = token
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        detail = e.read().decode()[:200] if hasattr(e, "read") else str(e)
        return {"_error": detail}


def parse_ts(value):
    """Meta returns offsets as '+0000'; datetime.fromisoformat only accepts
    '+00:00' before Python 3.11. Parsing must never fail silently here — a
    swallowed error turns the conversion-liveness check into a no-op."""
    if not value:
        return None
    if len(value) > 5 and value[-5] in "+-" and ":" not in value[-5:]:
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def event_count_7d(token, event_name):
    """Raw pixel events over 7 days. Counts browser and server copies separately
    (roughly 2x the deduplicated figure Events Manager shows), but it is the only
    volume signal that does not depend on ad attribution — so it still answers
    'is the event flowing at all'.

    Returns None, not 0, when the call fails: an unreachable API and a genuinely
    silent pixel are opposite problems, and reporting one as the other is how the
    2026-07-29 run announced "conversion has never fired" during a DNS outage."""
    start = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    res = get(token, f"{DATASET_ID}/stats", aggregation="event", start_time=start)
    if "_error" in res:
        return None
    total = 0
    for row in res.get("data", []):
        for entry in row.get("data", []):
            if entry.get("value") == event_name:
                total += int(entry.get("count", 0))
    return total


def action_count(actions, action_type):
    for a in actions or []:
        if a.get("action_type") == action_type:
            return int(float(a.get("value", 0)))
    return 0


def is_established(adset, now):
    created = parse_ts(adset.get("created_time"))
    return created is None or (now - created) >= timedelta(hours=36)


def main():
    token = load_token()
    now = datetime.now(timezone.utc)
    conv_action = f"offsite_conversion.custom.{CONVERSION_ID}"
    # api_errors stay separate from alerts: "we could not look" must never be
    # rendered as "your campaign is broken".
    lines, alerts, api_errors = [], [], []

    for cid, label in CAMPAIGNS.items():
        camp = get(token, cid, fields="name,effective_status,stop_time")
        if "_error" in camp:
            api_errors.append(f"{label}: {camp['_error'][:90]}")
            continue

        ins = get(token, f"{cid}/insights", date_preset="yesterday",
                  fields="spend,impressions,clicks,actions")
        if "_error" in ins:
            api_errors.append(f"{label} insights: {ins['_error'][:90]}")
            spend = clicks = taps = None
        else:
            row = (ins.get("data") or [{}])[0]
            spend = float(row.get("spend", 0) or 0)
            clicks = int(row.get("clicks", 0) or 0)
            taps = action_count(row.get("actions"), conv_action)

        sets = get(token, f"{cid}/adsets",
                   fields="name,effective_status,daily_budget,end_time,learning_stage_info,created_time")
        if "_error" in sets:
            api_errors.append(f"{label} ad sets: {sets['_error'][:90]}")
            active = []
            established = []
            budget = None
        else:
            active = [s for s in sets.get("data", []) if s.get("effective_status") == "ACTIVE"]
            established = [s for s in active if is_established(s, now)]
            budget = (
                sum(int(s.get("daily_budget") or 0) for s in established) / 100
                if active else None
            )

        lines.append(f"### {label}")
        metric_bits = [f"status: {camp.get('effective_status')}"]
        if spend is not None:
            spend_part = f"yesterday: ${spend:.2f}"
            if budget is not None:
                spend_part += f" of ${budget:.2f} established budget"
            metric_bits.append(spend_part)
            metric_bits.append(f"{clicks} clicks")
            metric_bits.append(f"{taps} store taps")
        lines.append("- " + " | ".join(metric_bits))
        if active and len(established) < len(active):
            skipped = len(active) - len(established)
            lines.append(f"- {skipped} active ad set(s) launched in the last 36h — excluded from pacing")

        if camp.get("effective_status") != "ACTIVE":
            alerts.append(f"{label} is {camp.get('effective_status')} — not delivering")
        elif sets.get("_error"):
            pass
        elif not active:
            alerts.append(f"{label} has no active ad sets — not delivering")
        elif spend is None:
            pass
        elif budget and spend < budget * UNDER_PACING:
            alerts.append(f"{label} underspent: ${spend:.2f} of ${budget:.2f}")
        elif budget and spend > budget * OVER_PACING:
            alerts.append(f"{label} overspent: ${spend:.2f} of ${budget:.2f}")

        for s in active:
            stage = (s.get("learning_stage_info") or {}).get("status")
            if stage:
                lines.append(f"- learning: {stage} ({s['name'][:40]})")
            ends = parse_ts(s.get("end_time"))
            if ends:
                days = (ends - now).days
                lines.append(f"- ends in {days}d ({ends.date()})")
                if days <= END_DATE_WARN_DAYS:
                    alerts.append(f"{label} ad set ends in {days}d — extend or it stops")

        ads = get(token, f"{cid}/ads", fields="name,effective_status,issues_info")
        if "_error" in ads:
            api_errors.append(f"{label} ads: {ads['_error'][:90]}")
            lines.append("")
            continue
        for ad in ads.get("data", []):
            if ad.get("issues_info"):
                summary = ad["issues_info"][0].get("error_summary", "issue")
                alerts.append(f"{label} ad '{ad['name'][:34]}': {summary}")
            elif ad.get("effective_status") in ("DISAPPROVED", "WITH_ISSUES"):
                alerts.append(f"{label} ad '{ad['name'][:34]}': {ad['effective_status']}")
        lines.append("")

    week = get(token, f"{ACCOUNT}/insights", date_preset="last_7d", fields="spend,actions")
    if "_error" in week:
        api_errors.append(f"account last 7 days: {week['_error'][:90]}")
    else:
        wrow = (week.get("data") or [{}])[0]
        wtaps = action_count(wrow.get("actions"), conv_action)
        wspend = float(wrow.get("spend", 0) or 0)
        # Account-wide spend over one campaign's attributed conversions would
        # overstate cost per tap, so per-campaign CPA is reported above instead.
        lines.append(f"### Last 7 days\n- ${wspend:.2f} account spend | {wtaps} attributed taps")
    raw = event_count_7d(token, "AppStoreClick")
    if raw is None:
        api_errors.append("pixel event stats unreachable")
    else:
        lines.append(f"- {raw} raw AppStoreClick events (browser + server, pre-dedup)")
        if raw < 100:
            alerts.append(f"AppStoreClick volume low ({raw} raw/wk) — may stall in learning")

    conv = get(token, CONVERSION_ID, fields="name,last_fired_time")
    if "_error" in conv:
        api_errors.append("conversion status unreachable")
    else:
        fired = parse_ts(conv.get("last_fired_time"))
        if fired:
            age = (now - fired).total_seconds() / 3600
            lines.append(f"- conversion last fired {age:.0f}h ago")
            if age > CONVERSION_QUIET_HOURS:
                alerts.append(f"conversion silent for {age:.0f}h — optimisation is blind")
        elif conv.get("last_fired_time"):
            alerts.append(f"could not parse conversion timestamp: {conv['last_fired_time']}")
        else:
            alerts.append("conversion has never fired")

    header = f"# Grow Guide ads — {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
    body = [header, ""]
    if api_errors:
        body += ["## COULD NOT CHECK (network or API, not the campaigns)"]
        body += [f"- {e}" for e in api_errors] + [""]
    if alerts:
        body += ["## NEEDS ATTENTION"] + [f"- {a}" for a in alerts] + [""]
    if not api_errors and not alerts:
        body += ["All clear.", ""]
    body += lines
    report = "\n".join(body)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{now.date()}.md").write_text(report)
    (LOG_DIR / "latest.md").write_text(report)
    print(report)

    if alerts or api_errors:
        if alerts:
            msg = alerts[0] if len(alerts) == 1 else f"{len(alerts)} issues — see latest.md"
        else:
            msg = "could not reach Meta — check skipped, campaigns not verified"
        # osascript rejects non-ASCII in an inline -e script, and most of these
        # messages carry an em dash — which would silently kill the notification
        # for exactly the alerts worth seeing.
        safe = msg.encode("ascii", "replace").decode("ascii")
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(safe)} with title "Grow Guide ads"'],
                       check=False)
    return 1 if (alerts or api_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
