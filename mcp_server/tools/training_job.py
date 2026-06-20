"""launch_training_job — creates a SageMaker training job with managed spot + checkpointing.

Real implementation added in T-07. Returns mock data until then.
"""
from __future__ import annotations
from pydantic import BaseModel, model_validator


class TrainingJobConfig(BaseModel):
    image_uri: str
    role_arn: str
    input_s3: str
    output_s3: str
    instance_type: str
    instance_count: int = 1
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


async def launch_training_job(config: TrainingJobConfig) -> LaunchResult:
    """Launch a SageMaker training job. Enables managed spot when use_spot=True."""
    # TODO (T-07): replace with sagemaker.create_training_job call
    return LaunchResult(
        job_id="cco-mock-job-001",
        region="us-west-2",
        arn="arn:aws:sagemaker:us-west-2:123456789012:training-job/cco-mock-job-001",
        status="InProgress",
    )
