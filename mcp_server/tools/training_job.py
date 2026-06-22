"""launch_training_job — creates a SageMaker training job with optional managed spot.

Job name is auto-generated as `cco-{unix_ts}-{6hex}` so the agent never has to supply one.
Managed spot + checkpoint are enabled together when use_spot=True (SageMaker requires both).
A CloudWatch metric is emitted on every successful launch for the T-16 dashboard.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, model_validator


# ── Config model ──────────────────────────────────────────────────────────────

class TrainingJobConfig(BaseModel):
    region: str
    image_uri: str
    role_arn: str
    input_s3: str
    output_s3: str
    instance_type: str
    instance_count: int = 1
    volume_size_gb: int = 30
    hyperparams: dict[str, str] = {}
    use_spot: bool = True
    max_run_sec: int = 86400
    max_wait_sec: int = 86400
    checkpoint_s3: str | None = None

    @model_validator(mode="after")
    def checkpoint_required_for_spot(self) -> "TrainingJobConfig":
        if self.use_spot and not self.checkpoint_s3:
            raise ValueError("checkpoint_s3 is required when use_spot=True")
        return self


class LaunchResult(BaseModel):
    job_id: str
    region: str
    arn: str
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_job_name() -> str:
    return f"cco-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _emit_launch_metric(region: str, instance_type: str) -> None:
    """Fire-and-forget CloudWatch metric — never raises."""
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        cw.put_metric_data(
            Namespace="CCO",
            MetricData=[{
                "MetricName": "JobsLaunched",
                "Dimensions": [{"Name": "InstanceType", "Value": instance_type}],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": datetime.now(timezone.utc),
            }],
        )
    except Exception:
        pass


# ── MCP tool ──────────────────────────────────────────────────────────────────

async def launch_training_job(config: TrainingJobConfig) -> LaunchResult:
    """Launch a SageMaker training job. Enables managed spot + checkpointing when use_spot=True.

    The job name is auto-generated with a `cco-` prefix. Returns job_id, ARN, and region
    so the agent can poll status via get_job_status.
    """
    sm = boto3.client("sagemaker", region_name=config.region)
    job_name = _generate_job_name()

    create_kwargs: dict = {
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
        "Tags": [{"Key": "ManagedBy", "Value": "cco-agent"}],
    }

    if config.use_spot and config.checkpoint_s3:
        create_kwargs["CheckpointConfig"] = {"S3Uri": config.checkpoint_s3}

    try:
        resp = sm.create_training_job(**create_kwargs)
    except ClientError as e:
        raise RuntimeError(f"SageMaker CreateTrainingJob failed: {e}") from e

    arn = resp["TrainingJobArn"]
    _emit_launch_metric(config.region, config.instance_type)

    return LaunchResult(job_id=job_name, region=config.region, arn=arn, status="InProgress")
