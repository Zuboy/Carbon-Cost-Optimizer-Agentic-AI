"""get_job_status — polls SageMaker DescribeTrainingJob for status and billable seconds.

Status mapping collapses SageMaker's 5 states into 4 simplified ones the agent acts on:
  InProgress  → InProgress  (also covers "Stopping")
  Completed   → Completed
  Failed      → Failed      (includes failure_reason)
  Stopped     → Stopped

Billable seconds: uses BillableTimeInSeconds (spot savings-aware) when present,
falling back to TrainingTimeInSeconds for on-demand jobs.
"""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel


# SageMaker status → simplified status
_STATUS_MAP: dict[str, str] = {
    "InProgress": "InProgress",
    "Stopping":   "InProgress",
    "Completed":  "Completed",
    "Failed":     "Failed",
    "Stopped":    "Stopped",
}


class JobStatusResult(BaseModel):
    job_id: str
    status: str                    # InProgress | Completed | Failed | Stopped
    billable_seconds: int | None = None
    instance_type: str | None = None
    region: str | None = None
    failure_reason: str | None = None


def _emit_completion_metric(region: str, status: str, billable_seconds: int | None) -> None:
    """Fire-and-forget CloudWatch metric for completed/failed jobs."""
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        metrics = [{
            "MetricName": f"Jobs{status}",
            "Value": 1,
            "Unit": "Count",
            "Timestamp": datetime.now(timezone.utc),
        }]
        if billable_seconds is not None:
            metrics.append({
                "MetricName": "BillableSeconds",
                "Value": float(billable_seconds),
                "Unit": "Seconds",
                "Timestamp": datetime.now(timezone.utc),
            })
        cw.put_metric_data(Namespace="CCO", MetricData=metrics)
    except Exception:
        pass


async def get_job_status(job_id: str, region: str | None = None) -> JobStatusResult:
    """Return current status and billable seconds for a SageMaker training job.

    region defaults to the Lambda's own region via AWS_DEFAULT_REGION.
    Raises RuntimeError if the job is not found.
    """
    resolved_region = region or boto3.session.Session().region_name or "us-east-1"
    sm = boto3.client("sagemaker", region_name=resolved_region)

    try:
        resp = sm.describe_training_job(TrainingJobName=job_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ValidationException":
            raise RuntimeError(f"Training job '{job_id}' not found.") from e
        raise RuntimeError(f"SageMaker DescribeTrainingJob failed: {e}") from e

    raw_status = resp.get("TrainingJobStatus", "")
    status = _STATUS_MAP.get(raw_status, raw_status)

    # BillableTimeInSeconds reflects spot savings; TrainingTimeInSeconds is wall-clock
    billable = resp.get("BillableTimeInSeconds") or resp.get("TrainingTimeInSeconds")
    instance_type = resp.get("ResourceConfig", {}).get("InstanceType")
    failure_reason = resp.get("FailureReason") if status == "Failed" else None

    result = JobStatusResult(
        job_id=job_id,
        status=status,
        billable_seconds=int(billable) if billable is not None else None,
        instance_type=instance_type,
        region=resolved_region,
        failure_reason=failure_reason,
    )

    if status in ("Completed", "Failed", "Stopped"):
        _emit_completion_metric(resolved_region, status, result.billable_seconds)

    return result
