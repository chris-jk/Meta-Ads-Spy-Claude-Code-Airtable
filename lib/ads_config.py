"""Account-specific ids for the monitoring and audience scripts.

The ad account number, the custom conversion id, the dataset (pixel) id and the
campaign ids identify one advertiser's account, and this repo is public, so
they never go in code. They live in a git-ignored JSON file:

    cp ads_config.example.json ads_config.json    # then fill in your ids

Load path, first match wins:

    1. the file named by the ADS_CONFIG environment variable
    2. ads_config.json at the repo root

Standard library only, so the launchd-run daily check can import it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO / "ads_config.json"
EXAMPLE_NAME = "ads_config.example.json"
# Every placeholder in the example file carries this marker, so a copied but
# unfilled config is caught instead of being sent to Meta as an id.
PLACEHOLDER_MARK = "YOUR_"


def config_path() -> Path:
    override = os.environ.get("ADS_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_PATH


def _unset(value) -> bool:
    """True for a missing, empty or still-placeholder value."""
    if value is None or value == "" or value == {}:
        return True
    if isinstance(value, str):
        return PLACEHOLDER_MARK in value
    if isinstance(value, dict):
        return any(PLACEHOLDER_MARK in str(k) for k in value)
    return False


def _read() -> dict | None:
    """The parsed file, or None when there is no file. Exits on a broken one."""
    path = config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"{path}: cannot read config ({type(e).__name__}); compare with {EXAMPLE_NAME}")
    if not isinstance(data, dict):
        sys.exit(f"{path}: expected a JSON object; compare with {EXAMPLE_NAME}")
    return data


def get(key: str, default=None):
    """One optional value: `default` when the file or the key is missing or unfilled."""
    data = _read() or {}
    value = data.get(key)
    return default if _unset(value) else value


def load(required: tuple[str, ...] = ()) -> dict:
    """The whole config. Exits with what to do when the file is missing or a
    required key is absent or still the example's placeholder."""
    path = config_path()
    data = _read()
    if data is None:
        sys.exit(f"no {path.name} found at {path}: copy {EXAMPLE_NAME} to "
                 f"ads_config.json and fill in your ids (or point ADS_CONFIG at a file)")
    missing = [k for k in required if _unset(data.get(k))]
    if missing:
        sys.exit(f"{path}: missing or placeholder value for {', '.join(missing)}; "
                 f"see {EXAMPLE_NAME}")
    account = data.get("ad_account")
    if account is not None and not _unset(account) and not str(account).startswith("act_"):
        sys.exit(f"{path}: ad_account must look like act_<id>")
    campaigns = data.get("campaigns")
    if campaigns is not None and not isinstance(campaigns, dict):
        sys.exit(f"{path}: campaigns must be an object of campaign id -> label")
    return data
