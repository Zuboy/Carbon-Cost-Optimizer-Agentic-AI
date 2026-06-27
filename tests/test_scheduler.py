"""Unit tests for schedule_deferred_job and cancel_deferred_job."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

import mcp_server.tools.scheduler as module
from mcp_server.tools.scheduler import (
    CancelResult,
    DeferredJobRequest,
    ScheduleResult,
    _build_sm_payload,
    cancel_deferred_job,
    schedule_deferred_job,
)
from mcp_server.tools.training_job import TrainingJobConfig

# ── Shared fixtures & helpers ──────────────────────────────────────────────────

FUTURE_ISO = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
MOCK_ARN = "arn:aws:scheduler:us-east-1:123456789012:schedule/cco/cco-mock"
SCHEDULER_ROLE = "arn:aws:iam::123456789012:role/EBSchedulerRole"

VALID_CONFIG = TrainingJobConfig(
    region="us-west-2",
    image_uri="763104351884.dkr.ecr.us-west-2.amazonaws.com/pytorch-training:2.1-gpu-py310",
    role_arn="arn:aws:iam::123456789012:role/SageMakerRole",
    input_s3="s3://cco-bucket/input/",
    output_s3="s3://cco-bucket/output/",
    instance_type="ml.g5.2xlarge",
    use_spot=True,
    checkpoint_s3="s3://cco-bucket/checkpoints/",
)


def _req(**overrides) -> DeferredJobRequest:
    base = dict(
        config=VALID_CONFIG,
        target_time_iso=FUTURE_ISO,
        scheduler_role_arn=SCHEDULER_ROLE,
    )
    base.update(overrides)
    return DeferredJobRequest(**base)


@pytest.fixture
def mock_eb():
    eb = MagicMock()
    eb.create_schedule.return_value = {"ScheduleArn": MOCK_ARN}
    with patch.object(module, "boto3") as mock_boto3:
        mock_boto3.client.return_value = eb
        yield eb


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ── DeferredJobRequest validation ──────────────────────────────────────────────

class TestDeferredJobRequestValidation:
    def test_valid_request_constructs(self):
        req = _req()
        assert req.target_time_iso == FUTURE_ISO

    def test_past_time_raises(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with pytest.raises(ValidationError, match="60 seconds in the future"):
            _req(target_time_iso=past)

    def test_too_soon_raises(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with pytest.raises(ValidationError, match="60 seconds in the future"):
            _req(target_time_iso=soon)

    def test_invalid_iso_raises(self):
        with pytest.raises(ValidationError, match="ISO-8601"):
            _req(target_time_iso="not-a-date")

    def test_default_region_is_us_east_1(self):
        assert _req().region == "us-east-1"


# ── _build_sm_payload ──────────────────────────────────────────────────────────

class TestBuildSmPayload:
    def test_job_name_in_payload(self):
        p = _build_sm_payload(VALID_CONFIG, "cco-test-job")
        assert p["TrainingJobName"] == "cco-test-job"

    def test_spot_sets_managed_spot_and_checkpoint(self):
        p = _build_sm_payload(VALID_CONFIG, "j")
        assert p["EnableManagedSpotTraining"] is True
        assert p["CheckpointConfig"] == {"S3Uri": "s3://cco-bucket/checkpoints/"}
        assert "MaxWaitTimeInSeconds" in p["StoppingCondition"]

    def test_no_spot_omits_checkpoint_and_max_wait(self):
        cfg = TrainingJobConfig(
            **{
                "region": "us-west-2",
                "image_uri": "img",
                "role_arn": "arn:aws:iam::x:role/r",
                "input_s3": "s3://b/i/",
                "output_s3": "s3://b/o/",
                "instance_type": "ml.m5.4xlarge",
                "use_spot": False,
            }
        )
        p = _build_sm_payload(cfg, "j")
        assert p["EnableManagedSpotTraining"] is False
        assert "CheckpointConfig" not in p
        assert "MaxWaitTimeInSeconds" not in p["StoppingCondition"]

    def test_deferred_tag_present(self):
        p = _build_sm_payload(VALID_CONFIG, "j")
        tags = {t["Key"]: t["Value"] for t in p["Tags"]}
        assert tags["LaunchMode"] == "deferred"
        assert tags["ManagedBy"] == "cco-agent"

    def test_image_uri_preserved(self):
        p = _build_sm_payload(VALID_CONFIG, "j")
        assert p["AlgorithmSpecification"]["TrainingImage"] == VALID_CONFIG.image_uri


# ── schedule_deferred_job — happy path ────────────────────────────────────────

class TestScheduleDeferredJob:
    @pytest.mark.asyncio
    async def test_returns_schedule_result(self, mock_eb):
        result = await schedule_deferred_job(_req())
        assert isinstance(result, ScheduleResult)

    @pytest.mark.asyncio
    async def test_schedule_name_has_cco_prefix(self, mock_eb):
        result = await schedule_deferred_job(_req())
        assert result.schedule_name.startswith("cco-")

    @pytest.mark.asyncio
    async def test_schedule_arn_from_response(self, mock_eb):
        result = await schedule_deferred_job(_req())
        assert result.schedule_arn == MOCK_ARN

    @pytest.mark.asyncio
    async def test_status_is_scheduled(self, mock_eb):
        result = await schedule_deferred_job(_req())
        assert result.status == "Scheduled"

    @pytest.mark.asyncio
    async def test_target_time_echoed(self, mock_eb):
        result = await schedule_deferred_job(_req())
        assert result.target_time == FUTURE_ISO

    @pytest.mark.asyncio
    async def test_creates_schedule_with_at_expression(self, mock_eb):
        await schedule_deferred_job(_req())
        call_kwargs = mock_eb.create_schedule.call_args.kwargs
        assert call_kwargs["ScheduleExpression"].startswith("at(")

    @pytest.mark.asyncio
    async def test_flexible_time_window_off(self, mock_eb):
        await schedule_deferred_job(_req())
        call_kwargs = mock_eb.create_schedule.call_args.kwargs
        assert call_kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}

    @pytest.mark.asyncio
    async def test_action_after_completion_delete(self, mock_eb):
        await schedule_deferred_job(_req())
        call_kwargs = mock_eb.create_schedule.call_args.kwargs
        assert call_kwargs["ActionAfterCompletion"] == "DELETE"

    @pytest.mark.asyncio
    async def test_target_arn_is_sagemaker_sdk(self, mock_eb):
        await schedule_deferred_job(_req())
        target = mock_eb.create_schedule.call_args.kwargs["Target"]
        assert "sagemaker:createTrainingJob" in target["Arn"]

    @pytest.mark.asyncio
    async def test_target_role_arn_from_request(self, mock_eb):
        await schedule_deferred_job(_req())
        target = mock_eb.create_schedule.call_args.kwargs["Target"]
        assert target["RoleArn"] == SCHEDULER_ROLE

    @pytest.mark.asyncio
    async def test_target_input_is_valid_json_sm_payload(self, mock_eb):
        await schedule_deferred_job(_req())
        target = mock_eb.create_schedule.call_args.kwargs["Target"]
        payload = json.loads(target["Input"])
        assert "TrainingJobName" in payload
        assert payload["TrainingJobName"].startswith("cco-")

    @pytest.mark.asyncio
    async def test_schedule_group_is_cco(self, mock_eb):
        await schedule_deferred_job(_req())
        call_kwargs = mock_eb.create_schedule.call_args.kwargs
        assert call_kwargs["GroupName"] == "cco"

    @pytest.mark.asyncio
    async def test_schedule_names_are_unique(self, mock_eb):
        r1 = await schedule_deferred_job(_req())
        r2 = await schedule_deferred_job(_req())
        assert r1.schedule_name != r2.schedule_name

    @pytest.mark.asyncio
    async def test_role_from_env_var(self, monkeypatch, mock_eb):
        monkeypatch.setenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::x:role/EnvRole")
        result = await schedule_deferred_job(_req(scheduler_role_arn=""))
        target = mock_eb.create_schedule.call_args.kwargs["Target"]
        assert target["RoleArn"] == "arn:aws:iam::x:role/EnvRole"
        assert result.status == "Scheduled"

    @pytest.mark.asyncio
    async def test_missing_role_raises(self, monkeypatch, mock_eb):
        monkeypatch.delenv("SCHEDULER_ROLE_ARN", raising=False)
        with pytest.raises(RuntimeError, match="SCHEDULER_ROLE_ARN"):
            await schedule_deferred_job(_req(scheduler_role_arn=""))

    @pytest.mark.asyncio
    async def test_client_error_raises_runtime_error(self, mock_eb):
        mock_eb.create_schedule.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(RuntimeError, match="EventBridge CreateSchedule failed"):
            await schedule_deferred_job(_req())


# ── cancel_deferred_job ────────────────────────────────────────────────────────

class TestCancelDeferredJob:
    @pytest.mark.asyncio
    async def test_returns_cancel_result(self, mock_eb):
        result = await cancel_deferred_job("cco-old-schedule")
        assert isinstance(result, CancelResult)
        assert result.status == "Cancelled"

    @pytest.mark.asyncio
    async def test_schedule_name_echoed(self, mock_eb):
        result = await cancel_deferred_job("cco-old-schedule")
        assert result.schedule_name == "cco-old-schedule"

    @pytest.mark.asyncio
    async def test_not_found_is_silent(self, mock_eb):
        mock_eb.delete_schedule.side_effect = _client_error("ResourceNotFoundException")
        result = await cancel_deferred_job("cco-already-fired")
        assert result.status == "Cancelled"

    @pytest.mark.asyncio
    async def test_other_client_error_raises(self, mock_eb):
        mock_eb.delete_schedule.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(RuntimeError, match="EventBridge DeleteSchedule failed"):
            await cancel_deferred_job("cco-schedule")

    @pytest.mark.asyncio
    async def test_delete_called_with_correct_group(self, mock_eb):
        await cancel_deferred_job("cco-test")
        mock_eb.delete_schedule.assert_called_once_with(Name="cco-test", GroupName="cco")
