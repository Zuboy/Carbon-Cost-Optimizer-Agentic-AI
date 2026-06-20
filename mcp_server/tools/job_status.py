"""get_job_status — polls SageMaker + CloudWatch for training job status and billable seconds.

Real implementation added in T-08. Returns mock data until then.
"""
from __future__ import annotations
from pydantic import BaseModel

# SageMaker status → simplified status
STATUS_MAP: dict[str, str] = {
    "InProgress": "InProgress",
    "Completed": "Completed",
    "Failed": "Failed",
    "Stopping": "InProgress",
    "Stopped": "Stopped",
}


class JobStatusResult(BaseModel):
    job_id: str
    status: str  # InProgress | Completed | Failed | Stopped
    billable_seconds: int | None = None
    instance_type: str | None = None
    region: str | None = None
    failure_reason: str | None = None


async def get_job_status(job_id: str) -> JobStatusResult:
    """Return current status and billable seconds for a SageMaker training job."""
    # TODO (T-08): replace with sagemaker.describe_training_job + CloudWatch
    return JobStatusResult(
        job_id=job_id,
        status="InProgress",
        billable_seconds=None,
        instance_type="ml.g5.2xlarge",
        region="us-west-2",
    )
