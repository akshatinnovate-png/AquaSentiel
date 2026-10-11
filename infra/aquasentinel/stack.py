"""The AquaSentinel AWS stack.

The shape of it, and why each service is here rather than being a logo on a
slide:

    ESP32 node
        |  MQTT over mutual TLS, QoS 1
        v
    IoT Core  --(topic rule)-->  Lambda: ingest  --> DynamoDB (devices/readings/alerts)
        |                             |
        |                             +--> SNS: alerts --> email / SMS
        |                             +--> EventBridge: alert.* events
        |                                        |
        |                                        +--> Lambda: explain (Bedrock)
        |                                        +--> Step Functions: escalation
        +--(topic rule)--> Firehose --> S3 data lake --> Glue/Athena

    EventBridge Scheduler --> Lambda: sweep   (detects nodes that went silent)
    API Gateway HTTP API  --> Lambda: api     (dashboard reads)
    S3 + CloudFront       --> the dashboard itself

Two deliberate choices worth defending:

* **IoT Core rather than an HTTP endpoint.** Each node gets its own X.509
  certificate, so a compromised node is revoked individually instead of
  rotating a shared key across a fleet. The HTTP path still exists, because a
  device that cannot do mutual TLS should still be able to report.

* **Both DynamoDB and S3.** DynamoDB answers "what is happening now" in
  single-digit milliseconds and expires raw telemetry after 30 days by TTL.
  Firehose writes the same records to S3 as partitioned Parquet-ready JSON, so
  the long history for tuning thresholds survives independently of the
  operational store, at about a thousandth of the cost per GB.
"""
from __future__ import annotations

import os
import pathlib

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as apigw_int,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cw,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_iot as iot,
    aws_kinesisfirehose as firehose,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
    aws_ssm as ssm,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Python 3.13 is current on Lambda and matches what the engine is tested on.
RUNTIME = lambda_.Runtime.PYTHON_3_13

IOT_TOPIC = "aquasentinel/telemetry"


class AquaSentinelStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        alert_email = self.node.try_get_context("alert_email")
        site_id = self.node.try_get_context("site_id") or "demo-site"
        enable_bedrock = str(self.node.try_get_context("enable_bedrock") or "true").lower() != "false"

        # ==============================================================
        # Storage
        # ==============================================================
        devices = ddb.Table(
            self, "Devices",
            partition_key=ddb.Attribute(name="device_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
        )

        readings = ddb.Table(
            self, "Readings",
            partition_key=ddb.Attribute(name="device_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="ts", type=ddb.AttributeType.NUMBER),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            # Raw telemetry is bulky and only operationally interesting for a
            # month. The full history lives in S3; this expires itself so the
            # hot store cannot grow without bound or without anyone noticing.
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        alerts = ddb.Table(
            self, "Alerts",
            partition_key=ddb.Attribute(name="alert_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
        )
        alerts.add_global_secondary_index(
            index_name="by_device",
            partition_key=ddb.Attribute(name="device_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="ts", type=ddb.AttributeType.NUMBER),
        )

        config_table = ddb.Table(
            self, "SiteConfig",
            partition_key=ddb.Attribute(name="site_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        lake = s3.Bucket(
            self, "DataLake",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(
                id="cheapen-old-telemetry",
                transitions=[s3.Transition(
                    storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                    transition_after=Duration.days(60),
                )],
            )],
        )

        # ==============================================================
        # Messaging
        # ==============================================================
        alert_topic = sns.Topic(self, "AlertTopic", display_name="AquaSentinel alerts")
        if alert_email:
            alert_topic.add_subscription(subs.EmailSubscription(alert_email))

        bus = events.EventBus(self, "EventBus", event_bus_name=f"aquasentinel-{Aws.ACCOUNT_ID}")

        # A dead-letter queue on every async Lambda. A telemetry record that
        # cannot be processed is evidence of a bug, and it should be waiting
        # for us in a queue rather than gone.
        dlq = sqs.Queue(self, "DeadLetters", retention_period=Duration.days(14),
                        enforce_ssl=True)

        # ==============================================================
        # Lambda
        # ==============================================================
        # One bundle for every function: the engine, the services and nothing
        # else. The engine has no third-party dependencies, so there is no
        # build step, no layer and no container -- which is also why the same
        # code can run on a laptop with no AWS account at all.
        code = lambda_.Code.from_asset(
            str(ROOT),
            exclude=[
                ".git", ".git/**", "infra", "infra/**", "web", "web/**",
                "tests", "tests/**", "docs", "docs/**", "sim", "sim/**",
                "**/__pycache__", "**/*.pyc", ".venv", ".venv/**",
                "eval/results/*.png", "node_modules", "node_modules/**",
            ],
        )

        common_env = {
            "DEVICES_TABLE": devices.table_name,
            "READINGS_TABLE": readings.table_name,
            "ALERTS_TABLE": alerts.table_name,
            "CONFIG_TABLE": config_table.table_name,
            "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            "EVENT_BUS_NAME": bus.event_bus_name,
            "SITE_ID": site_id,
            "READING_TTL_DAYS": "30",
            "PYTHONPATH": "/var/task",
        }

        def fn(name: str, handler: str, *, timeout_s: int = 30, memory: int = 512,
               env: dict | None = None, async_fn: bool = True) -> lambda_.Function:
            # An explicit log group rather than `log_retention`: the latter
            # provisions a custom resource and a second Lambda per function
            # just to set a retention policy, which is a lot of moving parts
            # for a two-week expiry.
            log_group = logs.LogGroup(
                self, f"{name}Logs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            )
            f = lambda_.Function(
                self, name,
                runtime=RUNTIME,
                handler=handler,
                code=code,
                timeout=Duration.seconds(timeout_s),
                memory_size=memory,
                environment={**common_env, **(env or {})},
                log_group=log_group,
                # X-Ray: when a reading takes too long to become an alert, the
                # trace says whether it was DynamoDB, the engine or SNS.
                tracing=lambda_.Tracing.ACTIVE,
                dead_letter_queue=dlq if async_fn else None,
                retry_attempts=1 if async_fn else None,
            )
            for table in (devices, readings, alerts, config_table):
                table.grant_read_write_data(f)
            return f

        ingest_fn = fn("IngestFn", "services.ingest.handler.handler",
                       timeout_s=30, memory=768)
        alert_topic.grant_publish(ingest_fn)
        bus.grant_put_events_to(ingest_fn)

        api_fn = fn("ApiFn", "services.api.handler.handler",
                    timeout_s=20, memory=512, async_fn=False)

        sweep_fn = fn("SweepFn", "services.forecast.handler.handler",
                      timeout_s=60, memory=512)
        alert_topic.grant_publish(sweep_fn)

        notify_fn = fn("NotifyFn", "services.notifier.handler.handler",
                       timeout_s=30, memory=256)
        alert_topic.grant_publish(notify_fn)

        explain_fn = fn("ExplainFn", "services.explain.handler.handler",
                        timeout_s=45, memory=512,
                        env={} if enable_bedrock else {"DISABLE_BEDROCK": "1"})
        if enable_bedrock:
            # Bedrock writes the plain-language summary of an alert. It is
            # scoped to invoking a model: it reads no tables and decides
            # nothing, which is the whole point of where it sits.
            explain_fn.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*",
                    f"arn:aws:bedrock:*:{Aws.ACCOUNT_ID}:inference-profile/*",
                ],
            ))

        # ==============================================================
        # IoT Core: the production uplink
        # ==============================================================
        policy = iot.CfnPolicy(
            self, "DevicePolicy",
            policy_name=f"aquasentinel-device-{Aws.REGION}",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["iot:Connect"],
                     # A certificate may only connect as its own thing name,
                     # so one node cannot impersonate another.
                     "Resource": [f"arn:aws:iot:{Aws.REGION}:{Aws.ACCOUNT_ID}:client/${{iot:Connection.Thing.ThingName}}"]},
                    {"Effect": "Allow", "Action": ["iot:Publish"],
                     "Resource": [f"arn:aws:iot:{Aws.REGION}:{Aws.ACCOUNT_ID}:topic/{IOT_TOPIC}/*"]},
                ],
            },
        )

        rule_role = iam.Role(
            self, "IotRuleRole",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com"),
        )
        ingest_fn.grant_invoke(iam.ServicePrincipal("iot.amazonaws.com"))

        iot.CfnTopicRule(
            self, "TelemetryToLambda",
            rule_name="aquasentinel_telemetry_to_lambda",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=f"SELECT * FROM '{IOT_TOPIC}/+'",
                aws_iot_sql_version="2016-03-23",
                description="Every telemetry message goes to the ingest function.",
                actions=[iot.CfnTopicRule.ActionProperty(
                    lambda_=iot.CfnTopicRule.LambdaActionProperty(
                        function_arn=ingest_fn.function_arn),
                )],
                error_action=iot.CfnTopicRule.ActionProperty(
                    sqs=iot.CfnTopicRule.SqsActionProperty(
                        queue_url=dlq.queue_url, role_arn=rule_role.role_arn, use_base64=False),
                ),
            ),
        )
        dlq.grant_send_messages(rule_role)

        # ==============================================================
        # Firehose -> S3 data lake, and a Glue table over it
        # ==============================================================
        fh_role = iam.Role(self, "FirehoseRole",
                           assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"))
        lake.grant_read_write(fh_role)
        fh_log_group = logs.LogGroup(self, "FirehoseLogs",
                                     retention=logs.RetentionDays.ONE_WEEK,
                                     removal_policy=RemovalPolicy.DESTROY)
        fh_log_stream = logs.LogStream(self, "FirehoseLogStream", log_group=fh_log_group,
                                       removal_policy=RemovalPolicy.DESTROY)
        fh_log_group.grant_write(fh_role)

        stream = firehose.CfnDeliveryStream(
            self, "TelemetryToLake",
            delivery_stream_name="aquasentinel-telemetry",
            delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream
            .ExtendedS3DestinationConfigurationProperty(
                bucket_arn=lake.bucket_arn,
                role_arn=fh_role.role_arn,
                # Hive-style partitions so Athena can prune by date instead of
                # scanning the whole lake for one afternoon's readings.
                prefix="telemetry/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
                error_output_prefix="telemetry-errors/!{firehose:error-output-type}/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=5),
                compression_format="GZIP",
                cloud_watch_logging_options=firehose.CfnDeliveryStream
                .CloudWatchLoggingOptionsProperty(
                    enabled=True, log_group_name=fh_log_group.log_group_name,
                    log_stream_name=fh_log_stream.log_stream_name),
            ),
        )

        fh_rule_role = iam.Role(self, "IotFirehoseRole",
                                assumed_by=iam.ServicePrincipal("iot.amazonaws.com"))
        fh_rule_role.add_to_policy(iam.PolicyStatement(
            actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
            resources=[stream.attr_arn],
        ))
        iot.CfnTopicRule(
            self, "TelemetryToFirehose",
            rule_name="aquasentinel_telemetry_to_lake",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=f"SELECT * FROM '{IOT_TOPIC}/+'",
                aws_iot_sql_version="2016-03-23",
                description="The same messages, archived to S3 for long-horizon analysis.",
                actions=[iot.CfnTopicRule.ActionProperty(
                    firehose=iot.CfnTopicRule.FirehoseActionProperty(
                        delivery_stream_name=stream.ref,
                        role_arn=fh_rule_role.role_arn,
                        separator="\n"),
                )],
            ),
        )

        glue_db = glue.CfnDatabase(
            self, "GlueDb",
            catalog_id=Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name="aquasentinel",
                description="AquaSentinel telemetry lake, queryable from Athena",
            ),
        )
        glue_table = glue.CfnTable(
            self, "GlueTable",
            catalog_id=Aws.ACCOUNT_ID,
            database_name="aquasentinel",
            table_input=glue.CfnTable.TableInputProperty(
                name="telemetry",
                table_type="EXTERNAL_TABLE",
                parameters={"classification": "json", "compressionType": "gzip"},
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="year", type="string"),
                    glue.CfnTable.ColumnProperty(name="month", type="string"),
                    glue.CfnTable.ColumnProperty(name="day", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{lake.bucket_name}/telemetry/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe"),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="device_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="timestamp", type="double"),
                        glue.CfnTable.ColumnProperty(name="seq", type="int"),
                        glue.CfnTable.ColumnProperty(name="buffered", type="boolean"),
                        glue.CfnTable.ColumnProperty(name="fw_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="rssi", type="int"),
                        glue.CfnTable.ColumnProperty(
                            name="readings",
                            type="struct<rain_mm:double,rain_rate_mmhr:double,"
                                 "street_water_cm:double,tank_level_cm:double,"
                                 "tank_level_pct:double,flow_lpm:double,"
                                 "temperature_c:double,humidity_pct:double>"),
                    ],
                ),
            ),
        )
        glue_table.node.add_dependency(glue_db)

        # ==============================================================
        # Scheduled sweep: the only way to notice silence
        # ==============================================================
        events.Rule(
            self, "SilenceSweep",
            description="Detect nodes that have stopped reporting, and refresh forecasts.",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(sweep_fn, dead_letter_queue=dlq)],
        )

        # ==============================================================
        # Step Functions: escalation for an unacknowledged critical alert
        # ==============================================================
        notify = sfn_tasks.LambdaInvoke(
            self, "SendFirstNotification",
            lambda_function=notify_fn,
            payload=sfn.TaskInput.from_object({
                "action": "notify",
                "alert_id": sfn.JsonPath.string_at("$.detail.alert_id"),
            }),
            result_path="$.notified",
        )
        wait = sfn.Wait(self, "WaitForAcknowledgement",
                        time=sfn.WaitTime.duration(Duration.minutes(30)))
        check = sfn_tasks.LambdaInvoke(
            self, "CheckAcknowledged",
            lambda_function=notify_fn,
            payload=sfn.TaskInput.from_object({
                "action": "check",
                "alert_id": sfn.JsonPath.string_at("$.detail.alert_id"),
            }),
            result_path="$.check",
        )
        resolved = sfn.Succeed(self, "Resolved",
                               comment="Someone acknowledged or the condition cleared.")
        escalated = sfn.Succeed(self, "Escalated",
                                comment="Nobody acknowledged; a second notification went out.")

        # The waiting is the reason this is a state machine and not a function:
        # thirty minutes of sleep inside Lambda would be thirty minutes of
        # billed idle time, and the state of each pending alert would be
        # invisible. Here it is a row in the console.
        machine = sfn.StateMachine(
            self, "EscalationFlow",
            definition_body=sfn.DefinitionBody.from_chainable(
                notify.next(wait).next(check).next(
                    sfn.Choice(self, "WasItAcknowledged?")
                    .when(sfn.Condition.boolean_equals("$.check.Payload.resolved", True), resolved)
                    .otherwise(escalated)
                )
            ),
            timeout=Duration.hours(4),
            tracing_enabled=True,
        )

        # Only critical alerts escalate. Everything else is on the dashboard.
        events.Rule(
            self, "CriticalAlertEscalation",
            event_bus=bus,
            description="Escalate a critical alert that nobody acknowledges.",
            event_pattern=events.EventPattern(
                source=["aquasentinel"],
                detail_type=events.Match.prefix("alert."),
                detail={"severity_label": ["CRITICAL"]},
            ),
            targets=[targets.SfnStateMachine(machine, dead_letter_queue=dlq)],
        )

        # Every new alert gets a plain-language summary written for it.
        events.Rule(
            self, "ExplainNewAlerts",
            event_bus=bus,
            description="Write a caretaker-readable summary of each new alert.",
            event_pattern=events.EventPattern(
                source=["aquasentinel"],
                detail_type=events.Match.prefix("alert."),
            ),
            targets=[targets.LambdaFunction(explain_fn, dead_letter_queue=dlq)],
        )

        # ==============================================================
        # API Gateway
        # ==============================================================
        http_api = apigw.HttpApi(
            self, "Api",
            description="AquaSentinel read API and HTTP ingest fallback",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST,
                               apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["content-type", "x-api-key"],
            ),
        )
        read_int = apigw_int.HttpLambdaIntegration("ReadIntegration", api_fn)
        for route in ("/overview", "/devices", "/devices/{proxy+}", "/alerts",
                      "/config", "/metrics", "/health"):
            http_api.add_routes(path=route, methods=[apigw.HttpMethod.GET], integration=read_int)
        http_api.add_routes(path="/alerts/{proxy+}", methods=[apigw.HttpMethod.POST],
                            integration=read_int)
        http_api.add_routes(path="/config", methods=[apigw.HttpMethod.POST], integration=read_int)
        http_api.add_routes(
            path="/ingest", methods=[apigw.HttpMethod.POST],
            integration=apigw_int.HttpLambdaIntegration("IngestIntegration", ingest_fn),
        )

        # ==============================================================
        # Dashboard hosting: S3 behind CloudFront
        # ==============================================================
        site_bucket = s3.Bucket(
            self, "DashboardBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self, "Dashboard",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                # Origin access control: the bucket stays private and only
                # CloudFront can read it.
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            comment="AquaSentinel dashboard",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        # The dashboard is static, so the API endpoint is injected at deploy
        # time as a tiny config file rather than baked into app.js.
        s3deploy.BucketDeployment(
            self, "DeployDashboard",
            sources=[
                s3deploy.Source.asset(str(ROOT / "web")),
                s3deploy.Source.data(
                    "config.js",
                    f"window.AQUASENTINEL_API = '{http_api.api_endpoint}';\n",
                ),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )

        # ==============================================================
        # Config, observability
        # ==============================================================
        ssm.StringParameter(
            self, "ApiEndpointParam",
            parameter_name=f"/aquasentinel/{site_id}/api-endpoint",
            string_value=http_api.api_endpoint,
            description="Where the simulator and any external client should POST telemetry.",
        )

        failures = cw.Alarm(
            self, "IngestFailures",
            metric=ingest_fn.metric_errors(period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            alarm_description="The ingest function is failing: telemetry is being lost.",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        failures.add_alarm_action(
            __import__("aws_cdk.aws_cloudwatch_actions", fromlist=["SnsAction"])
            .SnsAction(alert_topic)
        )
        cw.Alarm(
            self, "DeadLetterBacklog",
            metric=dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            alarm_description="Records are landing in the dead-letter queue.",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        dashboard = cw.Dashboard(self, "OpsDashboard", dashboard_name="AquaSentinel")
        dashboard.add_widgets(
            cw.GraphWidget(title="Telemetry ingested", width=12,
                           left=[ingest_fn.metric_invocations(statistic="Sum")]),
            cw.GraphWidget(title="Ingest errors and duration", width=12,
                           left=[ingest_fn.metric_errors(statistic="Sum")],
                           right=[ingest_fn.metric_duration(statistic="p95")]),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Dashboard API", width=12,
                           left=[api_fn.metric_invocations(statistic="Sum"),
                                 api_fn.metric_errors(statistic="Sum")]),
            cw.GraphWidget(title="Alert notifications published", width=12,
                           left=[alert_topic.metric_number_of_messages_published(statistic="Sum")]),
        )

        # ==============================================================
        # Outputs
        # ==============================================================
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint,
                  description="POST telemetry here, or point the dashboard at it")
        CfnOutput(self, "DashboardUrl", value=f"https://{distribution.distribution_domain_name}",
                  description="The AquaSentinel dashboard")
        CfnOutput(self, "IotTopic", value=f"{IOT_TOPIC}/<device_id>",
                  description="MQTT topic each node publishes to")
        CfnOutput(self, "IotPolicyName", value=policy.policy_name or "",
                  description="Attach this policy to each device certificate")
        CfnOutput(self, "DataLakeBucket", value=lake.bucket_name,
                  description="Long-horizon telemetry, queryable from Athena")
        CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn,
                  description="Subscribe an email or SMS endpoint here")
        CfnOutput(self, "EscalationStateMachine", value=machine.state_machine_arn,
                  description="Escalation workflow for unacknowledged critical alerts")
