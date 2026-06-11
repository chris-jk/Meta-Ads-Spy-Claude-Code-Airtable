"""Thin wrapper around the Meta Marketing API (Graph API v20.0).

Per-client usage: instantiate with an ad_account_id (e.g. 'act_123...') and
the shared System User token. All write calls return the API response dict.

Designed to fail loudly with the Meta error body included in the exception
message — no silent swallowing.
"""
from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

GRAPH_VERSION = "v20.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


class MarketingAPIError(RuntimeError):
    pass


def _check(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        raise MarketingAPIError(
            f"Meta API {resp.request.method} {resp.request.url} -> "
            f"{resp.status_code}: {resp.text}"
        )
    return resp.json()


@dataclass
class MarketingAPI:
    ad_account_id: str  # 'act_123...'
    page_id: str
    access_token: str
    instagram_account_id: str | None = None

    def _post(self, path: str, data: dict) -> dict:
        data = {**data, "access_token": self.access_token}
        return _check(requests.post(f"{BASE_URL}/{path}", data=data, timeout=60))

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = {**(params or {}), "access_token": self.access_token}
        return _check(requests.get(f"{BASE_URL}/{path}", params=params, timeout=60))

    # ---------- Campaign ----------
    def create_campaign(self, name: str, objective: str, status: str = "PAUSED") -> dict:
        return self._post(
            f"{self.ad_account_id}/campaigns",
            {
                "name": name,
                "objective": objective,
                "status": status,
                "special_ad_categories": "[]",
                "buying_type": "AUCTION",
                # Budget lives on the ad set (no campaign budget optimization),
                # so Meta requires this flag be set explicitly. False = ad sets
                # do not share budget.
                "is_adset_budget_sharing_enabled": "false",
            },
        )

    # ---------- Ad Set ----------
    def create_adset(
        self,
        campaign_id: str,
        name: str,
        daily_budget_usd: float,
        countries: list[str],
        age_min: int,
        age_max: int,
        genders: str,  # 'All' | 'Men' | 'Women'
        placements: str,  # 'Automatic' | 'Facebook + Instagram' | 'Facebook only' | 'Instagram only'
        objective: str,
        start_time: str | None = None,
        end_time: str | None = None,
        status: str = "PAUSED",
        interest_ids: list[str] | None = None,
        pixel_id: str | None = None,
        custom_event_type: str | None = None,
        optimization_goal: str | None = None,
    ) -> dict:
        targeting: dict[str, Any] = {
            "geo_locations": {"countries": countries},
            "age_min": age_min,
            "age_max": age_max,
        }
        if genders == "Men":
            targeting["genders"] = [1]
        elif genders == "Women":
            targeting["genders"] = [2]

        if placements == "Facebook + Instagram":
            targeting["publisher_platforms"] = ["facebook", "instagram"]
        elif placements == "Facebook only":
            targeting["publisher_platforms"] = ["facebook"]
        elif placements == "Instagram only":
            targeting["publisher_platforms"] = ["instagram"]
        # Automatic = omit publisher_platforms

        if interest_ids:
            targeting["flexible_spec"] = [
                {"interests": [{"id": i} for i in interest_ids]}
            ]

        # Optimization goal & billing event map to objective
        opt_goal, billing_event = _objective_to_optimization(objective)

        # Explicit override (e.g. LANDING_PAGE_VIEWS for a Traffic campaign that
        # would otherwise default to LINK_CLICKS). Conversion objectives below
        # still force OFFSITE_CONVERSIONS, so this only takes effect for
        # non-conversion objectives like OUTCOME_TRAFFIC.
        if optimization_goal:
            opt_goal = optimization_goal

        # Conversion objectives override optimization to OFFSITE_CONVERSIONS and
        # require a promoted_object pointing at the pixel + event.
        promoted_object = None
        if pixel_id and custom_event_type:
            opt_goal = "OFFSITE_CONVERSIONS"
            promoted_object = {
                "pixel_id": pixel_id,
                "custom_event_type": custom_event_type,
            }
        elif objective in ("OUTCOME_SALES", "OUTCOME_LEADS"):
            raise MarketingAPIError(
                f"{objective} requires a Pixel ID (on Client) and Pixel Event (on Campaign)."
            )

        payload: dict[str, Any] = {
            "name": name,
            "campaign_id": campaign_id,
            "daily_budget": int(round(daily_budget_usd * 100)),  # cents
            "billing_event": billing_event,
            "optimization_goal": opt_goal,
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": _json(targeting),
            "status": status,
        }
        if promoted_object:
            payload["promoted_object"] = _json(promoted_object)
        if start_time:
            payload["start_time"] = start_time
        if end_time:
            payload["end_time"] = end_time

        return self._post(f"{self.ad_account_id}/adsets", payload)

    # ---------- Creative ----------
    def upload_image(self, image_bytes: bytes, filename: str = "creative.jpg") -> str:
        """Upload image to ad account, return image_hash."""
        # Airtable download URLs carry no file extension, and an absent MIME type
        # makes requests send application/octet-stream, which Meta rejects with
        # FileTypeNotSupported. Detect the real type from the image's magic bytes.
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            mime, ext = "image/png", ".png"
        elif image_bytes[:3] == b"\xff\xd8\xff":
            mime, ext = "image/jpeg", ".jpg"
        elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime, ext = "image/gif", ".gif"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            mime, ext = "image/webp", ".webp"
        else:
            mime, ext = "image/jpeg", ".jpg"
        fname = (os.path.splitext(filename)[0] or "creative") + ext
        files = {fname: (fname, io.BytesIO(image_bytes), mime)}
        data = {"access_token": self.access_token}
        resp = requests.post(
            f"{BASE_URL}/{self.ad_account_id}/adimages",
            data=data,
            files=files,
            timeout=120,
        )
        body = _check(resp)
        images = body.get("images") or {}
        if not images:
            raise MarketingAPIError(f"Image upload returned no images: {body}")
        first = next(iter(images.values()))
        return first["hash"]

    def upload_video(self, video_bytes: bytes, filename: str = "creative.mp4") -> str:
        """Upload video to ad account, return video_id. Polls until processed."""
        files = {"source": (filename, io.BytesIO(video_bytes))}
        data = {"access_token": self.access_token}
        resp = requests.post(
            f"{BASE_URL}/{self.ad_account_id}/advideos",
            data=data,
            files=files,
            timeout=600,
        )
        body = _check(resp)
        video_id = body["id"]
        # Poll for ready
        for _ in range(60):
            time.sleep(5)
            status = self._get(video_id, {"fields": "status"})
            phase = (status.get("status") or {}).get("video_status")
            if phase == "ready":
                return video_id
            if phase == "error":
                raise MarketingAPIError(f"Video processing failed: {status}")
        raise MarketingAPIError(f"Video {video_id} not ready after 5 min")

    def create_image_creative(
        self,
        name: str,
        image_hash: str,
        headline: str,
        primary_text: str,
        description: str,
        cta: str,
        link_url: str,
    ) -> dict:
        link_data: dict[str, Any] = {
            "image_hash": image_hash,
            "link": link_url,
            "message": primary_text,
            "name": headline,
            "description": description,
            "call_to_action": {"type": cta, "value": {"link": link_url}},
        }
        object_story_spec: dict[str, Any] = {
            "page_id": self.page_id,
            "link_data": link_data,
        }
        if self.instagram_account_id:
            object_story_spec["instagram_actor_id"] = self.instagram_account_id

        return self._post(
            f"{self.ad_account_id}/adcreatives",
            {
                "name": name,
                "object_story_spec": _json(object_story_spec),
                # Meta deprecated the standard_enhancements opt-out field in
                # degrees_of_freedom_spec (error_subcode 3858504). Omitting it
                # lets the account's default enhancement settings apply.
            },
        )

    def create_video_creative(
        self,
        name: str,
        video_id: str,
        image_hash: str,  # thumbnail
        headline: str,
        primary_text: str,
        description: str,
        cta: str,
        link_url: str,
    ) -> dict:
        video_data: dict[str, Any] = {
            "video_id": video_id,
            "image_hash": image_hash,
            "message": primary_text,
            "title": headline,
            "link_description": description,
            "call_to_action": {"type": cta, "value": {"link": link_url}},
        }
        object_story_spec: dict[str, Any] = {
            "page_id": self.page_id,
            "video_data": video_data,
        }
        if self.instagram_account_id:
            object_story_spec["instagram_actor_id"] = self.instagram_account_id

        return self._post(
            f"{self.ad_account_id}/adcreatives",
            {"name": name, "object_story_spec": _json(object_story_spec)},
        )

    # ---------- Ad ----------
    def create_ad(self, adset_id: str, creative_id: str, name: str, status: str = "PAUSED") -> dict:
        return self._post(
            f"{self.ad_account_id}/ads",
            {
                "name": name,
                "adset_id": adset_id,
                "creative": _json({"creative_id": creative_id}),
                "status": status,
            },
        )

    # ---------- Targeting search ----------
    def search_interests(self, query: str, limit: int = 5) -> list[dict]:
        body = self._get(
            "search",
            {"type": "adinterest", "q": query, "limit": limit},
        )
        return body.get("data", [])


# ---------- helpers ----------
def _json(d: dict) -> str:
    import json
    return json.dumps(d)


def _objective_to_optimization(objective: str) -> tuple[str, str]:
    """Return (optimization_goal, billing_event) for a campaign objective."""
    mapping = {
        "OUTCOME_TRAFFIC": ("LINK_CLICKS", "IMPRESSIONS"),
        "OUTCOME_LEADS": ("LEAD_GENERATION", "IMPRESSIONS"),
        "OUTCOME_SALES": ("OFFSITE_CONVERSIONS", "IMPRESSIONS"),
        "OUTCOME_AWARENESS": ("REACH", "IMPRESSIONS"),
        "OUTCOME_ENGAGEMENT": ("POST_ENGAGEMENT", "IMPRESSIONS"),
        "OUTCOME_APP_PROMOTION": ("APP_INSTALLS", "IMPRESSIONS"),
    }
    if objective not in mapping:
        raise MarketingAPIError(f"Unsupported objective: {objective}")
    return mapping[objective]


def download(url: str) -> tuple[bytes, str]:
    """Download a URL, return (bytes, filename)."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    name = os.path.basename(urlparse(url).path) or "file"
    return resp.content, name


def verify_marketing_token(token: str, ad_account_id: str) -> dict:
    """Sanity check the token has access to the ad account."""
    resp = requests.get(
        f"{BASE_URL}/{ad_account_id}",
        params={"access_token": token, "fields": "id,name,account_status,currency"},
        timeout=30,
    )
    return _check(resp)
