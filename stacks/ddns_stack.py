"""CDK stack for long-shot-ddns AWS backend.

Provisions:
- Lambda function (src/handler.py)
- DynamoDB ownership table (first-claim, write-once semantics)
- REST API Gateway in front of the Lambda, API-key auth, 10 rps throttle
- ACM certificate for the custom domain (DNS-validated against the same zone)
- API Gateway custom domain + base-path mapping
- Route 53 A-alias from the custom domain to the API Gateway
- IAM scoped to one hosted zone + the ownership table

After deploy, retrieve the API key value with:
    aws apigateway get-api-key --api-key <ApiKeyId from outputs> \
                               --include-value --query value --output text
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_certificatemanager as acm,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
)
from constructs import Construct


class DdnsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        hosted_zone_id: str,
        hosted_zone_name: str,
        custom_domain: str,
        record_ttl: int = 300,
        alert_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- DynamoDB ownership table ---------------------------------------
        ownership_table = dynamodb.Table(
            self,
            "OwnershipTable",
            table_name="LongShotDdnsOwnership",
            partition_key=dynamodb.Attribute(
                name="hostname", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Lambda function ------------------------------------------------
        # Explicit log group instead of the deprecated log_retention= (which
        # provisions a hidden custom-resource Lambda just to set retention).
        handler_logs = logs.LogGroup(
            self,
            "DdnsHandlerLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        handler_fn = _lambda.Function(
            self,
            "DdnsHandler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("src"),
            timeout=Duration.seconds(15),
            memory_size=128,
            log_group=handler_logs,
            environment={
                "HOSTED_ZONE_ID": hosted_zone_id,
                "HOSTED_ZONE_NAME": hosted_zone_name,
                "RECORD_TTL": str(record_ttl),
                "OWNERSHIP_TABLE": ownership_table.table_name,
                # The API's own domain must never be updatable via the API —
                # a client could otherwise clobber the endpoint's A-alias.
                "RESERVED_HOSTNAMES": custom_domain,
            },
        )

        # --- IAM ------------------------------------------------------------
        zone_arn = f"arn:aws:route53:::hostedzone/{hosted_zone_id}"
        handler_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "route53:ChangeResourceRecordSets",
                    "route53:ListResourceRecordSets",
                ],
                resources=[zone_arn],
            )
        )
        # Ownership table: only the operations the handler needs.
        ownership_table.grant(handler_fn, "dynamodb:GetItem", "dynamodb:PutItem")

        # --- API Gateway + API key ------------------------------------------
        api = apigw.RestApi(
            self,
            "DdnsApi",
            rest_api_name="long-shot-ddns",
            description="Endpoint the Pi POSTs to with its current public IP.",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=10,
                throttling_burst_limit=20,
            ),
            # endpoint_types omitted — defaults to EDGE (CloudFront-fronted),
            # matching what was deployed before. Changing this would force a
            # replacement of the RestApi; not worth the disruption.
        )

        api.root.add_resource("update").add_method(
            "POST",
            apigw.LambdaIntegration(handler_fn),
            api_key_required=True,
        )

        api_key = api.add_api_key(
            "DdnsApiKey",
            api_key_name="long-shot-ddns-default",
            description="Default API key for the long-shot-ddns Pi client.",
        )

        usage_plan = api.add_usage_plan(
            "DdnsUsagePlan",
            name="long-shot-ddns-plan",
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        # --- Custom domain ---------------------------------------------------
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "ZoneLookup",
            hosted_zone_id=hosted_zone_id,
            zone_name=hosted_zone_name,
        )

        cert = acm.Certificate(
            self,
            "DdnsCert",
            domain_name=custom_domain,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        domain = apigw.DomainName(
            self,
            "DdnsDomain",
            domain_name=custom_domain,
            certificate=cert,
            endpoint_type=apigw.EndpointType.EDGE,
            security_policy=apigw.SecurityPolicy.TLS_1_2,
        )
        domain.add_base_path_mapping(api, stage=api.deployment_stage)

        route53.ARecord(
            self,
            "DdnsAliasRecord",
            zone=hosted_zone,
            record_name=custom_domain,
            target=route53.RecordTarget.from_alias(
                targets.ApiGatewayDomain(domain)
            ),
        )

        # --- Dead-Pi alarm (optional) -----------------------------------------
        # The client heartbeats at least once per 24h, so the Lambda is invoked
        # at least daily in steady state. No invocations for 30h (5 x 6h
        # periods, missing data = breaching) means the Pi is down or can't
        # reach the endpoint — email via SNS. Enabled only when alert_email
        # is set in cdk.context.json; the subscription must be confirmed once
        # via the email AWS sends.
        if alert_email:
            alert_topic = sns.Topic(
                self,
                "DdnsAlertTopic",
                display_name="long-shot-ddns alerts",
            )
            alert_topic.add_subscription(sns_subs.EmailSubscription(alert_email))

            heartbeat_alarm = cloudwatch.Alarm(
                self,
                "DdnsHeartbeatAlarm",
                alarm_name="long-shot-ddns-heartbeat-missing",
                alarm_description=(
                    "No long-shot-ddns Lambda invocations for 30h — the Pi has "
                    "stopped reporting (down, offline, or misconfigured). "
                    "Expected: at least one heartbeat POST every 24h."
                ),
                metric=handler_fn.metric_invocations(
                    period=Duration.hours(6),
                    statistic="Sum",
                ),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=5,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            )
            heartbeat_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))
            heartbeat_alarm.add_ok_action(cw_actions.SnsAction(alert_topic))

        # --- Outputs --------------------------------------------------------
        CfnOutput(
            self,
            "EndpointUrl",
            value=f"https://{custom_domain}/update",
            description="POST here from the Pi. Set as DDNS_ENDPOINT in /etc/long-shot-ddns/config.env.",
        )
        CfnOutput(
            self,
            "ApiGatewayDefaultUrl",
            value=f"{api.url}update",
            description="Auto-generated API Gateway URL (fallback / for debugging).",
        )
        CfnOutput(
            self,
            "ApiKeyId",
            value=api_key.key_id,
            description=(
                "Fetch the actual key value with: "
                "aws apigateway get-api-key --api-key <ApiKeyId> --include-value "
                "--query value --output text. Set as DDNS_API_KEY on the Pi."
            ),
        )
        CfnOutput(
            self,
            "OwnershipTableName",
            value=ownership_table.table_name,
            description=(
                "Inspect with: aws dynamodb scan --table-name <name>. "
                "Release a hostname with: aws dynamodb delete-item --table-name <name> "
                '--key \'{"hostname":{"S":"home.example.com"}}\''
            ),
        )
        CfnOutput(
            self,
            "LambdaFunctionName",
            value=handler_fn.function_name,
        )
