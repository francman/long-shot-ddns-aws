#!/usr/bin/env python3
"""CDK entry point for long-shot-ddns-aws.

Provisions the Lambda + API Gateway + IAM that the `long-shot-ddns` client on
a Raspberry Pi POSTs to. The Lambda updates a Route 53 A record.

Deploy:

    pip install -r requirements.txt
    cdk bootstrap                                              # one-time per account/region
    cdk deploy -c hosted_zone_id=Z123ABC... [-c record_ttl=300]

You can also persist the context values in `cdk.context.json` so you don't
have to pass -c every time.
"""
from __future__ import annotations

import os
import sys

import aws_cdk as cdk

from stacks.ddns_stack import DdnsStack


app = cdk.App()

# Apply project-level tags to every resource in every stack in this app.
# Filters in Cost Explorer, Resource Groups, etc. all key off these.
cdk.Tags.of(app).add("Project", "long-shot-ddns")
cdk.Tags.of(app).add("ManagedBy", "CDK")
# Repository tag is optional — set `repo_url` in cdk.context.json to enable.
# Useful so resources point back at the IaC that created them; left blank
# in the public template so personal fork URLs don't get committed.
_repo_url = app.node.try_get_context("repo_url")
if _repo_url:
    cdk.Tags.of(app).add("Repository", _repo_url)

hosted_zone_id = app.node.try_get_context("hosted_zone_id")
hosted_zone_name = app.node.try_get_context("hosted_zone_name")
custom_domain = app.node.try_get_context("custom_domain")
missing = [
    name for name, val in (
        ("hosted_zone_id", hosted_zone_id),
        ("hosted_zone_name", hosted_zone_name),
        ("custom_domain", custom_domain),
    ) if not val
]
if missing:
    print(
        f"error: missing required context value(s): {', '.join(missing)}. "
        "See cdk.context.json.example for the expected shape. "
        "Either copy it to cdk.context.json and edit, or pass values via -c.",
        file=sys.stderr,
    )
    sys.exit(2)

record_ttl = int(app.node.try_get_context("record_ttl") or 300)
# Optional: email for the dead-Pi heartbeat alarm. Omit to skip the alarm.
alert_email = app.node.try_get_context("alert_email")

DdnsStack(
    app,
    "LongShotDdnsStack",
    hosted_zone_id=hosted_zone_id,
    hosted_zone_name=hosted_zone_name,
    custom_domain=custom_domain,
    record_ttl=record_ttl,
    alert_email=alert_email,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="long-shot-ddns: Lambda + API Gateway + DynamoDB + custom domain that updates a Route 53 A record from a Raspberry Pi.",
)

app.synth()
