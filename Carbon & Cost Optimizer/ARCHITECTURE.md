# Carbon- & Cost-Aware SageMaker Training Orchestrator — Architecture

**Status:** Design v1 · **Date:** 2026-06-20 · **Agent framework:** AWS Strands Agents SDK

---

## 1. Goal

Given a request like *"train this model, deadline 6h, optimize for low carbon,"* the agent reasons over the **cost × carbon × deadline** tradeoff across candidate AWS regions and start times, picks the best (region, instance, start-time), and launches the SageMaker training job autonomously. Everything the agent can *do* is exposed as MCP tools.

---

## 2. Component Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  User request ("train X, deadline 6h, optimize carbon")            │
└───────────────┬────────────────────────────────────────────────────┘
                │
        ┌───────▼────────┐
        │  Strands Agent │  reasoning loop (LLM + system prompt + tools)
        │  on Bedrock    │  model: Claude on Amazon Bedrock
        │  AgentCore     │  ── plans, scores options, decides, launches
        └───────┬────────┘
                │ MCP (stdio / streamable-HTTP)
        ┌───────▼─────────────────────────────────────────────┐
        │  MCP Server  (Lambda + API Gateway, FastMCP)         │
        │  tools:                                              │
        │   • get_spot_prices(region, instance_type)           │
        │   • get_carbon_intensity(region)                     │
        │   • launch_training_job(config)                      │
        │   • get_job_status(job_id)                           │
        └───┬───────────┬──────────────┬────────────┬──────────┘
            │           │              │            │
   ┌────────▼──┐  ┌─────▼──────┐  ┌────▼──────┐ ┌───▼─────────┐
   │ EC2 Spot  │  │ WattTime / │  │ SageMaker │ │ SageMaker / │
   │ Price API │  │ ElecMaps   │  │ CreateTr- │ │ CloudWatch  │
   │ (boto3)   │  │ carbon API │  │ ainingJob │ │ Describe    │
   └───────────┘  └────────────┘  └───────────┘ └─────────────┘

   Deferred jobs ──► EventBridge Scheduler ──► re-invoke launch at greenest hour
```

---

## 3. MCP Server (Lambda-hosted)

Single Lambda fronted by API Gateway (or Lambda Function URL), built with **FastMCP** exposing `streamable-http` transport so the Strands agent connects over HTTP. One handler, four tools.

### Tool contracts

**`get_spot_prices(region, instance_type)`**
- Source: EC2 `describe_spot_price_history` (boto3), most recent price per AZ, plus On-Demand baseline from Pricing API.
- Returns: `{region, instance_type, az_prices:[{az, usd_per_hr}], on_demand_usd_per_hr, retrieved_at}`.

**`get_carbon_intensity(region)`**
- Maps AWS region → grid zone (e.g. `us-east-1`→`PJM`, `eu-west-1`→`IE`, `eu-north-1`→`SE`).
- Source: **WattTime** (marginal MOER, gCO₂/kWh — better signal for "should I run now?") with **Electricity Maps** as fallback/forecast (average + 24h forecast).
- Returns: `{region, zone, current_gco2_kwh, forecast:[{ts, gco2_kwh}], signal_type:"marginal"|"average"}`.

**`launch_training_job(config)`**
- Calls SageMaker `create_training_job` with `config` (image, input/output S3, instance_type, hyperparams, use_spot, max_run, max_wait).
- Enables **managed spot** (`EnableManagedSpotTraining=true`) + checkpointing when spot is chosen.
- Returns: `{job_id, region, arn, status:"InProgress"}`.

**`get_job_status(job_id)`**
- SageMaker `describe_training_job` + CloudWatch metrics.
- Returns: `{job_id, status, billable_seconds, instance_type, region}`.

> ⚠️ **Carbon API caveat (verified):** WattTime's *free* tier only returns marginal data for `CAISO_NORTH`; other zones need WattTime Pro. Electricity Maps free tier is **non-commercial** and returns the **average** (not marginal) signal. For a free PoC, restrict candidate regions to what the free tier covers, or budget for a paid key. This constraint should be encoded in the region candidate list.

---

## 4. Agent Layer (Strands SDK)

Strands is model-driven: **model + system prompt + tools**. It has native MCP client support, so it discovers the four tools from the MCP server at runtime — no per-tool glue code.

**Runtime:** Amazon **Bedrock AgentCore** serverless runtime — supports long-running/async tool execution and MCP natively. Good fit since launch + monitoring spans hours.

**Reasoning loop:**
1. Parse request → extract `{job_spec, deadline, objective_weights}`.
2. For each candidate region: call `get_spot_prices` + `get_carbon_intensity`.
3. Score each (region, start-time) option (see §5).
4. If best option is "defer to a greener hour within deadline," register an **EventBridge Scheduler** one-time schedule instead of launching now.
5. Otherwise call `launch_training_job`; return plan + rationale to user.
6. Monitor via `get_job_status` (polled or event-driven).

---

## 5. Decision Logic

Estimate job energy: `kWh ≈ est_runtime_hr × instance_power_kW` (lookup table per instance type, e.g. p4d/g5/trn1 TDP-derived).

Per candidate option *i* (region × start-time within deadline):

```
cost_i    = price_usd_per_hr_i  × est_runtime_hr
carbon_i  = gco2_kwh_i          × kWh
feasible  = (start_time_i + est_runtime_hr) ≤ deadline
```

Normalize cost and carbon to [0,1] across feasible options, then:

```
score_i = w_cost · norm(cost_i) + w_carbon · norm(carbon_i)      (lower = better)
```

`w_cost`, `w_carbon` come from the user's objective ("optimize for low carbon" → `w_carbon≈0.8`). The agent picks `argmin score_i` among feasible options and explains the tradeoff (e.g. "running in eu-north-1 at 02:00 cuts carbon 61% for +4% cost, finishes 90 min before deadline").

---

## 6. Scheduling (defer-to-greenest-hour)

When the optimal start is in the future, the agent creates a **one-time EventBridge Scheduler** entry targeting the MCP server's launch path (or a thin launch Lambda) with the resolved config. This decouples "decide now" from "run later" and survives agent session end. Re-evaluation at fire time is optional (carbon forecasts drift).

---

## 7. AWS Services & IAM (least-privilege)

| Concern | Service | Key permissions |
|---|---|---|
| Reasoning LLM | Bedrock (Claude) | `bedrock:InvokeModel` |
| Agent runtime | Bedrock AgentCore | runtime-scoped |
| Tools host | Lambda + API Gateway | execution role below |
| Training | SageMaker | `sagemaker:CreateTrainingJob`, `DescribeTrainingJob` |
| Pricing/spot | EC2 / Pricing | `ec2:DescribeSpotPriceHistory`, `pricing:GetProducts` |
| Scheduling | EventBridge Scheduler | `scheduler:CreateSchedule` |
| Monitoring | CloudWatch | `cloudwatch:GetMetricData`, Logs |
| Data | S3 | scoped to training bucket prefixes |

- MCP Lambda role: only the action-tool permissions above; **no** `iam:PassRole` beyond the specific SageMaker execution role ARN.
- Carbon API keys in **Secrets Manager**, not env vars.
- SageMaker execution role separate from the Lambda role (passed via scoped `PassRole`).

---

## 8. Open Decisions

1. **Region candidate set** — gated by carbon-API coverage/cost (see §3 caveat). Start with 3–4 regions the free tier covers.
2. **Energy model fidelity** — static per-instance power table vs. measured GPU utilization from CloudWatch.
3. **Spot interruption strategy** — managed spot + checkpointing assumed; confirm `max_wait` policy.
4. **Re-evaluate at fire time** — recompute carbon when a deferred job actually launches, or trust the original forecast.

---

## 9. Next Build Steps

1. MCP server skeleton (FastMCP, 4 tools, mock data) → deploy to Lambda.
2. Strands agent wired to the MCP endpoint + scoring function.
3. EventBridge Scheduler integration for deferral.
4. End-to-end test against a tiny real SageMaker job.
