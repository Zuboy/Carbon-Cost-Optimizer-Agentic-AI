"""CCO CDK Stack — Lambda MCP server, API Gateway HTTP API, IAM roles, S3 training bucket."""
from __future__ import annotations
import json
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct


class CCOStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── S3: training data bucket ─────────────────────────────────────────
        training_bucket = s3.Bucket(
            self,
            "TrainingBucket",
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireOldOutputs",
                    prefix="output/",
                    expiration=Duration.days(90),
                ),
                s3.LifecycleRule(
                    id="TransitionCheckpoints",
                    prefix="checkpoints/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(30),
                        )
                    ],
                ),
            ],
        )

        # ── IAM: SageMaker execution role (used by training jobs, not Lambda) ─
        sagemaker_role = iam.Role(
            self,
            "SageMakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description="Execution role for CCO-launched SageMaker training jobs",
        )
        sagemaker_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess")
        )
        training_bucket.grant_read_write(sagemaker_role)

        # ── EventBridge Scheduler: group + execution role for deferred launches ──
        # Deferred jobs target SageMaker CreateTrainingJob directly via the SDK target;
        # EventBridge assumes this role at fire time. Schedules live in the `cco` group.
        scheduler.CfnScheduleGroup(self, "CCOScheduleGroup", name="cco")

        scheduler_role = iam.Role(
            self,
            "SchedulerExecutionRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Role EventBridge Scheduler assumes to launch deferred SageMaker jobs",
        )
        scheduler_role.add_to_policy(iam.PolicyStatement(
            sid="CreateDeferredTrainingJob",
            actions=["sagemaker:CreateTrainingJob", "sagemaker:AddTags"],
            resources=[f"arn:aws:sagemaker:*:{self.account}:training-job/cco-*"],
        ))
        # CreateTrainingJob passes the SageMaker execution role — scoped PassRole
        scheduler_role.add_to_policy(iam.PolicyStatement(
            sid="PassSageMakerRoleAtFireTime",
            actions=["iam:PassRole"],
            resources=[sagemaker_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
        ))

        # ── IAM: MCP Lambda execution role (least-privilege) ─────────────────
        lambda_role = iam.Role(
            self,
            "MCPLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for CCO MCP server Lambda — least-privilege",
        )
        lambda_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="SpotPrices",
            actions=["ec2:DescribeSpotPriceHistory"],
            resources=["*"],
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="OnDemandPricing",
            actions=["pricing:GetProducts"],
            resources=["*"],
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="SageMakerTrainingJobs",
            actions=[
                "sagemaker:CreateTrainingJob",
                "sagemaker:DescribeTrainingJob",
                "sagemaker:StopTrainingJob",
            ],
            # Scoped to jobs launched by this agent (cco- prefix)
            resources=[f"arn:aws:sagemaker:*:{self.account}:training-job/cco-*"],
        ))

        # PassRole scoped to SageMaker only, for this specific role
        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="PassSageMakerRole",
            actions=["iam:PassRole"],
            resources=[sagemaker_role.role_arn],
            conditions={
                "StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}
            },
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="EventBridgeScheduler",
            actions=[
                "scheduler:CreateSchedule",
                "scheduler:GetSchedule",
                "scheduler:DeleteSchedule",
            ],
            resources=[f"arn:aws:scheduler:*:{self.account}:schedule/cco/*"],
        ))

        # Setting Target.RoleArn on a schedule requires PassRole on the scheduler role,
        # scoped to the scheduler service so it can't be passed elsewhere.
        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="PassSchedulerRole",
            actions=["iam:PassRole"],
            resources=[scheduler_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="CloudWatchMetrics",
            actions=["cloudwatch:PutMetricData", "cloudwatch:GetMetricData"],
            resources=["*"],
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="CarbonAPISecrets",
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:/cco/*"],
        ))

        lambda_role.add_to_policy(iam.PolicyStatement(
            sid="CCOSSMParams",
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:*:{self.account}:parameter/cco/*"],
        ))

        training_bucket.grant_read_write(lambda_role)

        # ── Lambda: MCP server ────────────────────────────────────────────────
        mcp_lambda = lambda_.Function(
            self,
            "MCPServerLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="mcp_server.app.lambda_handler",
            code=lambda_.Code.from_asset(
                ".",  # repo root — bundling installs requirements-lambda.txt
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        (
                            "pip install -r requirements-lambda.txt -t /asset-output --quiet && "
                            "cp -r mcp_server /asset-output/"
                        ),
                    ],
                ),
            ),
            role=lambda_role,
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                "POWERTOOLS_SERVICE_NAME": "cco-mcp-server",
                "SAGEMAKER_ROLE_ARN": sagemaker_role.role_arn,
                "SCHEDULER_ROLE_ARN": scheduler_role.role_arn,
                "TRAINING_BUCKET": training_bucket.bucket_name,
            },
        )

        # ── API Gateway HTTP API (streamable-HTTP MCP transport) ──────────────
        http_api = apigwv2.HttpApi(
            self,
            "MCPHttpApi",
            description="CCO MCP Server — streamable-HTTP transport for Strands agent",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_origins=["*"],
                allow_headers=["content-type", "x-amzn-trace-id", "mcp-session-id"],
            ),
        )
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=integrations.HttpLambdaIntegration(
                "MCPIntegration",
                mcp_lambda,
                payload_format_version=apigwv2.PayloadFormatVersion.VERSION_2_0,
            ),
        )

        # ── Secrets Manager: carbon API keys (placeholder — update post-deploy) ─
        secretsmanager.Secret(
            self,
            "WattTimeSecret",
            secret_name="/cco/watttime/credentials",
            description="WattTime API credentials — update username/password after deploy",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "REPLACE_ME"}),
                generate_string_key="password",
            ),
        )
        secretsmanager.Secret(
            self,
            "ElectricityMapsSecret",
            secret_name="/cco/electricity-maps/api-key",
            description="Electricity Maps API key — update api_key after deploy",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"api_key": "REPLACE_ME"}),
                generate_string_key="_unused",
            ),
        )

        # ── SSM Parameters: runtime config ────────────────────────────────────
        ssm.StringParameter(
            self,
            "CandidateRegions",
            parameter_name="/cco/candidate-regions",
            description="Candidate AWS regions for training job placement",
            string_value=json.dumps(["us-west-2", "eu-north-1", "ca-central-1"]),
        )
        ssm.StringParameter(
            self,
            "InstancePowerKW",
            parameter_name="/cco/instance-power-kw",
            description="Per-instance power draw in kW (TDP-derived) for carbon estimation",
            string_value=json.dumps({
                "ml.g5.2xlarge": 0.30,
                "ml.g5.12xlarge": 1.20,
                "ml.g5.48xlarge": 4.80,
                "ml.p4d.24xlarge": 6.50,
                "ml.trn1.32xlarge": 5.60,
                "ml.m5.4xlarge": 0.12,
                "ml.c5.4xlarge": 0.09,
            }),
        )

        # ── CloudFormation outputs ─────────────────────────────────────────────
        cdk.CfnOutput(
            self, "MCPServerURL",
            value=http_api.url or "",
            description="Paste into MCP_SERVER_URL in .env",
        )
        cdk.CfnOutput(
            self, "TrainingBucketName",
            value=training_bucket.bucket_name,
            description="S3 bucket for training input/output/checkpoints",
        )
        cdk.CfnOutput(
            self, "SageMakerRoleARN",
            value=sagemaker_role.role_arn,
            description="SageMaker execution role ARN — pass in launch_training_job config",
        )
        cdk.CfnOutput(
            self, "SchedulerRoleARN",
            value=scheduler_role.role_arn,
            description="EventBridge Scheduler execution role — used by schedule_deferred_job",
        )
        cdk.CfnOutput(
            self, "LambdaFunctionName",
            value=mcp_lambda.function_name,
            description="MCP Lambda — use for cdk deploy --hotswap during dev",
        )
