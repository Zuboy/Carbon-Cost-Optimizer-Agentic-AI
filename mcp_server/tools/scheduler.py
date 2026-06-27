"""schedule_deferred_job / cancel_deferred_job — EventBridge Scheduler for deferred launches.

When the optimal start time is in the future the agent calls schedule_deferred_job.
EventBridge fires at target_time and calls SageMaker CreateTrainingJob directly via the
universal SDK target — no extra Lambda needed at fire time.

Schedule naming: `cco-{unix_ts}-{6hex}` placed in schedule group `cco`.
ActionAfterCompletion=DELETE auto-cleans the schedule once it fires, so cancel_deferred_job
is only needed if the user changes their mind before the fire time.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, field_validator

from mcp_server.tools.training_job import TrainingJobConfig

SCHEDULE_GROUP = "cco"
_SM_SDK_TARGET = "arn:aws:scheduler:::aws-sdk:sagemaker:createTrainingJob"


class DeferredJobRequest(BaseModel):
    config: TrainingJobConfig
    target_time_iso: str          # ISO-8601 UTC, e.g. "2026-01-02T03:00:00Z"
    scheduler_role_arn: str = ""  # IAM role EventBridge assumes to call SageMaker; falls back to env
    region: str = "us-east-1"    # region for the EventBridge Scheduler service

    @field_validator("target_time_iso")
    @classmethod
    def must_be_future(cls, v: str) -> str:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"target_time_iso must be ISO-8601, got: {v!r}")
        if dt <= datetime.now(timezone.utc) + timedelta(seconds=60):
            raise ValueError("target_time_iso must be at least 60 seconds in the future")
        return v


class ScheduleResult(BaseModel):
    schedule_name: str
    schedule_arn: str
    target_time: str
    status: str = "Scheduled"


class CancelResult(BaseModel):
    schedule_name: str
    status: str = "Cancelled"


def _make_name() -> str:
    return f"cco-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _resolve_scheduler_role(provided: str) -> str:
    role = provided or os.environ.get("SCHEDULER_ROLE_ARN", "")
    if not role:
        raise RuntimeError(
            "scheduler_role_arn not supplied and SCHEDULER_ROLE_ARN env var is not set"
        )
    return role


def _build_sm_payload(config: TrainingJobConfig, job_name: str) -> dict:
    """Translate TrainingJobConfig → SageMaker CreateTrainingJob API payload (PascalCase)."""
    payload: dict = {
        "TrainingJobName": job_name,
        "AlgorithmSpecification": {
            "TrainingImage": config.image_uri,
            "TrainingInputMode": "File",
        },
        "RoleArn": config.role_arn,
        "InputDataConfig": [{
            "ChannelName": "training",
            "DataSource": {
                "S3DataSource": {
                    "S3DataType": "S3Prefix",
                    "S3Uri": config.input_s3,
                    "S3DataDistributionType": "FullyReplicated",
                }
            },
        }],
        "OutputDataConfig": {"S3OutputPath": config.output_s3},
        "ResourceConfig": {
            "InstanceType": config.instance_type,
            "InstanceCount": config.instance_count,
            "VolumeSizeInGB": config.volume_size_gb,
        },
        "StoppingCondition": {
            "MaxRuntimeInSeconds": config.max_run_sec,
            **({"MaxWaitTimeInSeconds": config.max_wait_sec} if config.use_spot else {}),
        },
        "EnableManagedSpotTraining": config.use_spot,
        "HyperParameters": config.hyperparams,
        "Tags": [
            {"Key": "ManagedBy", "Value": "cco-agent"},
            {"Key": "LaunchMode", "Value": "deferred"},
        ],
    }
    if config.use_spot and config.checkpoint_s3:
        payload["CheckpointConfig"] = {"S3Uri": config.checkpoint_s3}
    return payload


async def schedule_deferred_job(request: DeferredJobRequest) -> ScheduleResult:
    """Register a one-time EventBridge Scheduler entry to launch a SageMaker job at a future time.

    EventBridge calls SageMaker CreateTrainingJob directly at target_time_iso.
    The schedule auto-deletes after firing (ActionAfterCompletion=DELETE).
    Returns the schedule_name so the agent can surface it to the user or cancel it later.
    """
    role_arn = _resolve_scheduler_role(request.scheduler_role_arn)
    eb = boto3.client("scheduler", region_name=request.region)

    schedule_name = _make_name()
    job_name = _make_name()

    dt = datetime.fromisoformat(request.target_time_iso.replace("Z", "+00:00"))
    at_expr = f"at({dt.strftime('%Y-%m-%dT%H:%M:%S')})"

    try:
        resp = eb.create_schedule(
            Name=schedule_name,
            GroupName=SCHEDULE_GROUP,
            ScheduleExpression=at_expr,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": _SM_SDK_TARGET,
                "RoleArn": role_arn,
                "Input": json.dumps(_build_sm_payload(request.config, job_name)),
                "RetryPolicy": {
                    "MaximumRetryAttempts": 2,
                    "MaximumEventAgeInSeconds": 3600,
                },
            },
            ActionAfterCompletion="DELETE",
        )
    except ClientError as e:
        raise RuntimeError(f"EventBridge CreateSchedule failed: {e}") from e

    return ScheduleResult(
        schedule_name=schedule_name,
        schedule_arn=resp["ScheduleArn"],
        target_time=request.target_time_iso,
        status="Scheduled",
    )


async def cancel_deferred_job(schedule_name: str, region: str = "us-east-1") -> CancelResult:
    """Cancel a deferred job schedule before it fires.

    Safe to call if the schedule already fired and was auto-deleted — returns Cancelled regardless.
    """
    eb = boto3.client("scheduler", region_name=region)
    try:
        eb.delete_schedule(Name=schedule_name, GroupName=SCHEDULE_GROUP)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            pass  # already fired and auto-deleted
        else:
            raise RuntimeError(f"EventBridge DeleteSchedule failed: {e}") from e
    return CancelResult(schedule_name=schedule_name, status="Cancelled")
