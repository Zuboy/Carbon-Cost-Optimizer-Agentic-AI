"""Unit tests for launch_training_job — all boto3 calls mocked."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import ValidationError

import mcp_server.tools.training_job as module
from mcp_server.tools.training_job import LaunchResult, TrainingJobConfig, launch_training_job

# ── Fixtures ──────────────────────────────────────────────────────────────────

VALID_CONFIG = dict(
    region="us-west-2",
    image_uri="763104351884.dkr.ecr.us-west-2.amazonaws.com/pytorch-training:2.1-gpu-py310",
    role_arn="arn:aws:iam::123456789012:role/SageMakerRole",
    input_s3="s3://cco-bucket/input/",
    output_s3="s3://cco-bucket/output/",
    instance_type="ml.g5.2xlarge",
    use_spot=True,
    checkpoint_s3="s3://cco-bucket/checkpoints/",
)

MOCK_ARN = "arn:aws:sagemaker:us-west-2:123456789012:training-job/cco-mock"


@pytest.fixture
def mock_sm():
    sm = MagicMock()
    sm.create_training_job.return_value = {"TrainingJobArn": MOCK_ARN}
    cw = MagicMock()

    def _client(service, **kwargs):
        return sm if service == "sagemaker" else cw

    with patch.object(module, "boto3") as mock_boto3:
        mock_boto3.client.side_effect = _client
        yield sm, cw


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_launch_result(mock_sm):
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert isinstance(result, LaunchResult)


@pytest.mark.asyncio
async def test_job_id_has_cco_prefix(mock_sm):
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert result.job_id.startswith("cco-")


@pytest.mark.asyncio
async def test_job_id_unique_across_calls(mock_sm):
    r1 = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    r2 = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert r1.job_id != r2.job_id


@pytest.mark.asyncio
async def test_returns_arn_from_sagemaker(mock_sm):
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert result.arn == MOCK_ARN


@pytest.mark.asyncio
async def test_status_is_in_progress(mock_sm):
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert result.status == "InProgress"


@pytest.mark.asyncio
async def test_region_echoed_in_result(mock_sm):
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert result.region == "us-west-2"


# ── Managed spot configuration ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spot_enables_managed_spot_flag(mock_sm):
    sm, _ = mock_sm
    await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    kwargs = sm.create_training_job.call_args.kwargs
    assert kwargs["EnableManagedSpotTraining"] is True


@pytest.mark.asyncio
async def test_spot_sets_checkpoint_config(mock_sm):
    sm, _ = mock_sm
    await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    kwargs = sm.create_training_job.call_args.kwargs
    assert kwargs["CheckpointConfig"] == {"S3Uri": "s3://cco-bucket/checkpoints/"}


@pytest.mark.asyncio
async def test_spot_sets_max_wait_in_stopping_condition(mock_sm):
    sm, _ = mock_sm
    await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    kwargs = sm.create_training_job.call_args.kwargs
    assert "MaxWaitTimeInSeconds" in kwargs["StoppingCondition"]


@pytest.mark.asyncio
async def test_no_spot_omits_checkpoint_and_max_wait(mock_sm):
    sm, _ = mock_sm
    config = TrainingJobConfig(**{**VALID_CONFIG, "use_spot": False, "checkpoint_s3": None})
    await launch_training_job(config)
    kwargs = sm.create_training_job.call_args.kwargs
    assert kwargs["EnableManagedSpotTraining"] is False
    assert "CheckpointConfig" not in kwargs
    assert "MaxWaitTimeInSeconds" not in kwargs["StoppingCondition"]


# ── Tags & CloudWatch ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_tagged_with_managed_by(mock_sm):
    sm, _ = mock_sm
    await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    tags = sm.create_training_job.call_args.kwargs["Tags"]
    assert {"Key": "ManagedBy", "Value": "cco-agent"} in tags


@pytest.mark.asyncio
async def test_cloudwatch_metric_emitted(mock_sm):
    _, cw = mock_sm
    await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    cw.put_metric_data.assert_called_once()
    call_kwargs = cw.put_metric_data.call_args.kwargs
    assert call_kwargs["Namespace"] == "CCO"
    assert call_kwargs["MetricData"][0]["MetricName"] == "JobsLaunched"


@pytest.mark.asyncio
async def test_cloudwatch_failure_does_not_raise(mock_sm):
    _, cw = mock_sm
    cw.put_metric_data.side_effect = Exception("CW down")
    # Should not propagate — metric is fire-and-forget
    result = await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
    assert result.status == "InProgress"


# ── Validation ────────────────────────────────────────────────────────────────

def test_spot_without_checkpoint_raises():
    with pytest.raises(ValidationError, match="checkpoint_s3 is required"):
        TrainingJobConfig(**{**VALID_CONFIG, "checkpoint_s3": None})


def test_on_demand_without_checkpoint_is_valid():
    cfg = TrainingJobConfig(**{**VALID_CONFIG, "use_spot": False, "checkpoint_s3": None})
    assert cfg.use_spot is False


# ── SageMaker error handling ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sagemaker_client_error_raises_runtime_error(mock_sm):
    from botocore.exceptions import ClientError
    sm, _ = mock_sm
    sm.create_training_job.side_effect = ClientError(
        {"Error": {"Code": "ResourceLimitExceeded", "Message": "limit"}}, "CreateTrainingJob"
    )
    with pytest.raises(RuntimeError, match="SageMaker CreateTrainingJob failed"):
        await launch_training_job(TrainingJobConfig(**VALID_CONFIG))
