"""Pre-flight config verifier — checks every secret and SSM param is set and non-placeholder.

Run before invoking the agent or deploying to confirm the environment is ready.

Usage:
    python scripts/verify_config.py [--region us-east-1]
    make verify-config
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import boto3
from botocore.exceptions import ClientError

PLACEHOLDER_VALUES = {"REPLACE_ME", "", "your_username", "your_password", "your_api_key"}

# (display_name, aws_service, resource_id, required_keys)
CHECKS: list[tuple[str, str, str, list[str]]] = [
    # Secrets Manager
    ("WattTime credentials",       "secret",  "/cco/watttime/credentials",      ["username", "password"]),
    ("Electricity Maps API key",   "secret",  "/cco/electricity-maps/api-key",  ["api_key"]),
    # SSM Parameters
    ("Candidate regions",          "ssm",     "/cco/candidate-regions",         []),
    ("Instance power table (kW)",  "ssm",     "/cco/instance-power-kw",         []),
]


def _check_secret(client, secret_id: str, required_keys: list[str]) -> tuple[bool, str]:
    try:
        resp = client.get_secret_value(SecretId=secret_id)
        value = json.loads(resp["SecretString"])
        for key in required_keys:
            if str(value.get(key, "")) in PLACEHOLDER_VALUES:
                return False, f"key '{key}' is still a placeholder"
        return True, "ok"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        return False, f"AWS error: {code}"


def _check_ssm(client, param_name: str) -> tuple[bool, str]:
    try:
        resp = client.get_parameter(Name=param_name)
        raw = resp["Parameter"]["Value"]
        parsed = json.loads(raw)
        if not parsed:
            return False, "empty value"
        return True, f"{len(parsed)} item(s)" if isinstance(parsed, (list, dict)) else "ok"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        return False, f"AWS error: {code}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify CCO config in Secrets Manager and SSM")
    parser.add_argument("--region", default=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"))
    args = parser.parse_args()

    secrets_client = boto3.client("secretsmanager", region_name=args.region)
    ssm_client = boto3.client("ssm", region_name=args.region)

    print(f"\nCCO — config pre-flight check ({args.region})")
    print("=" * 55)

    all_ok = True
    rows = []
    for name, svc, resource_id, required_keys in CHECKS:
        if svc == "secret":
            ok, detail = _check_secret(secrets_client, resource_id, required_keys)
        else:
            ok, detail = _check_ssm(ssm_client, resource_id)
        status = "✓" if ok else "✗"
        rows.append((status, name, resource_id, detail))
        if not ok:
            all_ok = False

    # Print aligned table
    max_name = max(len(r[1]) for r in rows)
    for status, name, resource_id, detail in rows:
        print(f"  {status}  {name:<{max_name}}  {resource_id}  ({detail})")

    print()
    if all_ok:
        print("All checks passed — ready to deploy.\n")
    else:
        print("Some checks failed. Run `make update-secrets` to fix missing credentials.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
