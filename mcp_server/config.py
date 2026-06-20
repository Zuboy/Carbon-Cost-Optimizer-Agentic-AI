"""Config loader — reads candidate regions, power table, and API keys from SSM / Secrets Manager.

Env vars take precedence so local dev works without hitting AWS:
  CANDIDATE_REGIONS   comma-separated list, e.g. "us-west-2,eu-north-1"
  INSTANCE_POWER_TABLE  JSON dict, e.g. '{"ml.g5.2xlarge":0.30}'
  WATTTIME_USERNAME / WATTTIME_PASSWORD
  ELECTRICITY_MAPS_API_KEY

In Lambda the region comes from AWS_DEFAULT_REGION (set automatically by the runtime).
"""
from __future__ import annotations
import json
import os
from functools import lru_cache
import boto3

_REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("CDK_DEFAULT_REGION", "us-east-1")


@lru_cache(maxsize=1)
def _ssm():
    return boto3.client("ssm", region_name=_REGION)


@lru_cache(maxsize=1)
def _secrets():
    return boto3.client("secretsmanager", region_name=_REGION)


def get_candidate_regions() -> list[str]:
    if env := os.environ.get("CANDIDATE_REGIONS"):
        return [r.strip() for r in env.split(",") if r.strip()]
    resp = _ssm().get_parameter(Name="/cco/candidate-regions")
    return json.loads(resp["Parameter"]["Value"])


def get_instance_power_kw() -> dict[str, float]:
    if env := os.environ.get("INSTANCE_POWER_TABLE"):
        return json.loads(env)
    resp = _ssm().get_parameter(Name="/cco/instance-power-kw")
    return json.loads(resp["Parameter"]["Value"])


def get_watttime_credentials() -> dict[str, str]:
    if username := os.environ.get("WATTTIME_USERNAME"):
        return {"username": username, "password": os.environ["WATTTIME_PASSWORD"]}
    resp = _secrets().get_secret_value(SecretId="/cco/watttime/credentials")
    return json.loads(resp["SecretString"])


def get_electricity_maps_key() -> str:
    if key := os.environ.get("ELECTRICITY_MAPS_API_KEY"):
        return key
    resp = _secrets().get_secret_value(SecretId="/cco/electricity-maps/api-key")
    return json.loads(resp["SecretString"])["api_key"]
