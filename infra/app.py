#!/usr/bin/env python3
"""CDK entry point for the AquaSentinel stack.

    cd infra
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    cdk bootstrap          # once per account/region
    cdk deploy

Context values (all optional, pass with -c key=value):
    alert_email=you@example.com   subscribe an address to the alert topic
    site_id=my-school             logical site id
    enable_bedrock=false          skip the Bedrock explainer and its IAM grant
"""
import os

import aws_cdk as cdk

from aquasentinel.stack import AquaSentinelStack

app = cdk.App()

AquaSentinelStack(
    app,
    "AquaSentinel",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="AquaSentinel: water risk monitoring (flood, loss, supply, heat demand)",
)

cdk.Tags.of(app).add("project", "aquasentinel")
cdk.Tags.of(app).add("managed-by", "cdk")

app.synth()
