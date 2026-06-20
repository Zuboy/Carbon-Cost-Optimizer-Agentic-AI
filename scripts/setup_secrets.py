"""Post-deploy helper — push real carbon API credentials into Secrets Manager.

Run once after `make deploy` to replace the CDK-created placeholder values.
Reads credentials from environment variables (load your .env first).

Usage:
    export $(grep -v '^#' .env | xargs)   # load .env
    python scripts/setup_secrets.py [--region us-east-1] [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import boto3
from botocore.exceptions import ClientError


def update_secret(client, secret_id: str, value: dict, dry_run: bool) -> None:
    payload = json.dumps(value)
    if dry_run:
        print(f"  [dry-run] would update {secret_id}")
        return
    try:
        client.put_secret_value(SecretId=secret_id, SecretString=payload)
        print(f"  ✓ updated {secret_id}")
    except ClientError as e:
        print(f"  ✗ failed to update {secret_id}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Push carbon API credentials into Secrets Manager")
    parser.add_argument("--region", default=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--dry-run", action="store_true", help="Print what would change, don't write")
    args = parser.parse_args()

    secrets = boto3.client("secretsmanager", region_name=args.region)

    print(f"\nCCO — updating Secrets Manager in {args.region}")
    print("=" * 50)

    # ── WattTime credentials ──────────────────────────────────────────────────
    wt_user = os.environ.get("WATTTIME_USERNAME", "")
    wt_pass = os.environ.get("WATTTIME_PASSWORD", "")
    if not wt_user or not wt_pass:
        print("  ✗ WATTTIME_USERNAME / WATTTIME_PASSWORD not set — skipping", file=sys.stderr)
    else:
        update_secret(
            secrets,
            "/cco/watttime/credentials",
            {"username": wt_user, "password": wt_pass},
            args.dry_run,
        )

    # ── Electricity Maps API key ──────────────────────────────────────────────
    em_key = os.environ.get("ELECTRICITY_MAPS_API_KEY", "")
    if not em_key:
        print("  ✗ ELECTRICITY_MAPS_API_KEY not set — skipping", file=sys.stderr)
    else:
        update_secret(
            secrets,
            "/cco/electricity-maps/api-key",
            {"api_key": em_key},
            args.dry_run,
        )

    print("\nDone. Run `make verify-config` to confirm all values are set.\n")


if __name__ == "__main__":
    main()
