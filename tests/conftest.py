"""Shared pytest fixtures — fake AWS credentials so moto never hits real AWS."""
import pytest


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")


@pytest.fixture
def candidate_regions(monkeypatch):
    monkeypatch.setenv("CANDIDATE_REGIONS", "us-west-2,eu-north-1,ca-central-1")
    return ["us-west-2", "eu-north-1", "ca-central-1"]
