#!/usr/bin/env python3
"""Create the 'Clients' and 'Campaigns' tables in an Airtable base.

Campaigns links to Clients via a multipleRecordLinks field, so Clients
must be created first.

Usage:
    python3 setup_campaigns_tables.py --base-id appXXXXXXXXXXXXXXX
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SCHEMA_PATH = Path(__file__).parent / "campaigns_schema.json"


def existing_tables(base):
    try:
        return {t.name: t for t in base.schema().tables}
    except Exception as e:
        print(f"Warning: could not read base schema ({e}).")
        return {}


def create_one_table(base, table_def, linked_table_ids):
    name = table_def["table_name"]
    fields = []
    for f in table_def["fields"]:
        f = json.loads(json.dumps(f))  # deep copy
        # Resolve linkedTableName -> linkedTableId at create time
        if f.get("type") == "multipleRecordLinks":
            opts = f.get("options", {})
            link_name = opts.pop("linkedTableName", None)
            if link_name:
                if link_name not in linked_table_ids:
                    sys.exit(
                        f"Field '{f['name']}' links to '{link_name}' but that table "
                        f"hasn't been created yet."
                    )
                opts["linkedTableId"] = linked_table_ids[link_name]
            f["options"] = opts
        fields.append(f)

    print(f"Creating table '{name}' with {len(fields)} fields...")
    table = base.create_table(
        name,
        fields=fields,
        description=table_def.get("description", ""),
    )
    print(f"  Created (id: {table.id})")
    return table


def main():
    parser = argparse.ArgumentParser(description="Create Clients + Campaigns tables in Airtable")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"),
                        help="Airtable base ID (defaults to AIRTABLE_BASE_ID env var)")
    args = parser.parse_args()
    if not args.base_id:
        sys.exit("No base ID — pass --base-id or set AIRTABLE_BASE_ID in .env")

    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        sys.exit("AIRTABLE_PAT not set in .env — add it and try again.")

    from pyairtable import Api

    api = Api(pat)
    base = api.base(args.base_id)

    with open(SCHEMA_PATH) as f:
        schema_def = json.load(f)

    existing = existing_tables(base)
    linked_ids = {name: t.id for name, t in existing.items()}

    for table_def in schema_def["tables"]:
        name = table_def["table_name"]
        if name in existing:
            t = existing[name]
            print(f"Table '{name}' already exists (id: {t.id}). Skipping.")
            linked_ids[name] = t.id
            continue
        try:
            t = create_one_table(base, table_def, linked_ids)
            linked_ids[name] = t.id
        except Exception as e:
            sys.exit(f"Failed to create table '{name}': {e}")

    print("\nDone. Next: add at least one row to Clients (your ad account + page),")
    print("then a row to Campaigns with Status='Ready to launch', then run create_campaigns.py.")


if __name__ == "__main__":
    main()
