"""Config loader — reads candidate regions, power table, and API keys from SSM / Secrets Manager.

Env vars override SSM for local dev (set in .env).
"""
from __future__ import annotations
import json
import os
import boto3

_ssm = None
_secrets = None


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_secrets():
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets


def get_candidate_regions() -> list[str]:
    if env := os.environ.get("CANDIDATE_REGIONS"):
        return env.split(",")
    resp = _get_ssm().get_parameter(Name="/cco/candidate-regions")
    return json.loads(resp["Parameter"]["Value"])


def get_instance_power_kw() -> dict[str, float]:
    if env := os.environ.get("INSTANCE_POWER_TABLE"):
        return json.loads(env)
    resp = _get_ssm().get_parameter(Name="/cco/instance-power-kw")
    return json.loads(resp["Parameter"]["Value"])


def get_watttime_credentials() -> dict[str, str]:
    if username := os.environ.get("WATTTIME_USERNAME"):
        return {"username": username, "password": os.environ["WATTTIME_PASSWORD"]}
    resp = _get_secrets().get_secret_value(SecretId="/cco/watttime/credentials")
    return json.loads(resp["SecretString"])


def get_electricity_maps_key() -> str:
    if key := os.environ.get("ELECTRICITY_MAPS_API_KEY"):
        return key
    resp = _get_secrets().get_secret_value(SecretId="/cco/electricity-maps/api-key")
    return json.loads(resp["SecretString"])["api_key"]
