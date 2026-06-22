"""Unit tests for get_job_status — all boto3 calls mocked."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import mcp_server.tools.job_status as module
from mcp_server.tools.job_status import JobStatusResult, get_job_status

# ── Helpers ───────────────────────────────────────────────────────────────────

def _sm_response(
    status: str = "InProgress",
    billable: int | None = None,
    training_time: int | None = None,
    instance_type: str = "ml.g5.2xlarge",
    failure_reason: str | None = None,
) -> dict:
    resp: dict = {
        "TrainingJobStatus": status,
        "ResourceConfig": {"InstanceType": instance_type},
    }
    if billable is not None:
        resp["BillableTimeInSeconds"] = billable
    if training_time is not None:
        resp["TrainingTimeInSeconds"] = training_time
    if failure_reason:
        resp["FailureReason"] = failure_reason
    return resp


@pytest.fixture
def mock_sm():
    sm = MagicMock()
    cw = MagicMock()
    session_mock = MagicMock()
    session_mock.region_name = "us-west-2"

    def _client(service, **kwargs):
        return sm if service == "sagemaker" else cw

    with patch.object(module, "boto3") as mock_boto3:
        mock_boto3.client.side_effect = _client
        mock_boto3.session.Session.return_value = session_mock
        yield sm, cw


# ── Happy path — status mapping ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_job_status_result(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response("InProgress")
    result = await get_job_status("cco-test-job")
    assert isinstance(result, JobStatusResult)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [
    ("InProgress", "InProgress"),
    ("Stopping",   "InProgress"),   # Stopping collapses to InProgress
    ("Completed",  "Completed"),
    ("Failed",     "Failed"),
    ("Stopped",    "Stopped"),
])
async def test_status_mapping(mock_sm, raw, expected):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response(raw)
    result = await get_job_status("cco-test-job")
    assert result.status == expected


# ── Billable seconds ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uses_billable_time_when_present(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response(
        "Completed", billable=1800, training_time=3600
    )
    result = await get_job_status("cco-test-job")
    assert result.billable_seconds == 1800  # prefers BillableTimeInSeconds


@pytest.mark.asyncio
async def test_falls_back_to_training_time(mock_sm):
    sm, _ = mock_sm
    # No BillableTimeInSeconds (on-demand job)
    sm.describe_training_job.return_value = _sm_response("Completed", training_time=3600)
    result = await get_job_status("cco-test-job")
    assert result.billable_seconds == 3600


@pytest.mark.asyncio
async def test_billable_seconds_none_when_in_progress(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response("InProgress")
    result = await get_job_status("cco-test-job")
    assert result.billable_seconds is None


# ── Failure reason ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_reason_returned_when_failed(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response(
        "Failed", failure_reason="OutOfMemoryError"
    )
    result = await get_job_status("cco-test-job")
    assert result.failure_reason == "OutOfMemoryError"


@pytest.mark.asyncio
async def test_failure_reason_none_when_not_failed(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response("Completed")
    result = await get_job_status("cco-test-job")
    assert result.failure_reason is None


# ── Instance type & region ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_instance_type_extracted(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response("InProgress", instance_type="ml.p4d.24xlarge")
    result = await get_job_status("cco-test-job")
    assert result.instance_type == "ml.p4d.24xlarge"


@pytest.mark.asyncio
async def test_region_passed_through(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.return_value = _sm_response("InProgress")
    result = await get_job_status("cco-test-job", region="eu-north-1")
    assert result.region == "eu-north-1"


# ── CloudWatch metrics ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cw_metric_emitted_on_completion(mock_sm):
    sm, cw = mock_sm
    sm.describe_training_job.return_value = _sm_response("Completed", billable=1800)
    await get_job_status("cco-test-job")
    cw.put_metric_data.assert_called_once()


@pytest.mark.asyncio
async def test_cw_metric_not_emitted_while_in_progress(mock_sm):
    sm, cw = mock_sm
    sm.describe_training_job.return_value = _sm_response("InProgress")
    await get_job_status("cco-test-job")
    cw.put_metric_data.assert_not_called()


@pytest.mark.asyncio
async def test_cw_failure_does_not_raise(mock_sm):
    sm, cw = mock_sm
    sm.describe_training_job.return_value = _sm_response("Completed", billable=900)
    cw.put_metric_data.side_effect = Exception("CW down")
    result = await get_job_status("cco-test-job")
    assert result.status == "Completed"


# ── Error cases ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_not_found_raises_runtime_error(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "not found"}},
        "DescribeTrainingJob",
    )
    with pytest.raises(RuntimeError, match="not found"):
        await get_job_status("cco-nonexistent-job")


@pytest.mark.asyncio
async def test_other_client_error_raises_runtime_error(mock_sm):
    sm, _ = mock_sm
    sm.describe_training_job.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "DescribeTrainingJob",
    )
    with pytest.raises(RuntimeError, match="SageMaker DescribeTrainingJob failed"):
        await get_job_status("cco-test-job")
