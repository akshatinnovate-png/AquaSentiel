# AWS services, and what each one earns its place doing

17 services, 100 CloudFormation resources, all defined in `infra/aquasentinel/stack.py`.
Reproduce the inventory with:

```bash
cd infra && npx cdk synth > /tmp/t.yaml
grep -oE 'AWS::[A-Za-z0-9]+' /tmp/t.yaml | sort | uniq -c | sort -rn
```

The test is not "how many services can we name" — it is whether removing one
would cost us something real. Each row below says what breaks without it.

| Service | Role | What breaks without it |
|---|---|---|
| **IoT Core** | MQTT ingress with per-device mutual TLS; two topic rules fan one message to Lambda and to Firehose | A shared API key across a fleet: one compromised node means rotating every node. Also loses QoS 1 delivery and the MQTT test client as a demo surface. |
| **Lambda** | 5 functions: ingest, api, sweep, notify, explain | — |
| **DynamoDB** | 4 tables: devices (+ engine state), readings (TTL 30 d), alerts (+ GSI by device), config | Single-digit-ms reads for "what is happening now", and the TTL that stops raw telemetry growing without bound. |
| **API Gateway (HTTP API)** | Dashboard reads + an HTTP ingest fallback | Devices that cannot do mutual TLS could not report, and the dashboard would need direct AWS credentials. |
| **S3** | Telemetry data lake, dashboard hosting | Long history for threshold tuning, at ~1/1000 the cost per GB of the hot store. |
| **Kinesis Data Firehose** | Buffers and GZIPs telemetry into date-partitioned S3 prefixes | Writing one object per reading: thousands of tiny files, and Athena scanning all of them. |
| **Glue Data Catalog** | Database + external table over the lake | Athena would have no schema; every analysis query would start with a `CREATE TABLE`. |
| **Athena** | Ad-hoc SQL over the lake (no resource; it reads the Glue table) | Threshold tuning would mean exporting and loading data by hand. |
| **SNS** | Alert fan-out to email/SMS, and CloudWatch alarm actions | Alerts would exist only on a dashboard nobody is looking at. |
| **EventBridge** | A custom bus for `alert.*` events, plus a 5-minute scheduled sweep | **The sweep is the only way to notice silence.** No arriving message ever reports that a node has stopped reporting. |
| **Step Functions** | Escalation: notify → wait 30 min → check acknowledgement → escalate | Thirty minutes of billed idle Lambda, and no visibility into which alerts are waiting on whom. |
| **SQS** | Dead-letter queue for every async Lambda and for the IoT rule | A telemetry record that cannot be processed would be gone rather than waiting for inspection. |
| **Amazon Bedrock** | Plain-language alert summaries | Caretakers would read "minimum_night_flow_lpm 0.31 > 0.25". It explains; it never decides. |
| **CloudFront** | HTTPS + CDN in front of the private dashboard bucket | The bucket would have to be public. Origin access control keeps it private. |
| **CloudWatch** | Log groups (2-week retention), an ops dashboard, alarms on ingest errors and DLQ depth | Nobody would know the ingest function had started failing. |
| **X-Ray** | Active tracing on every function | When a reading takes too long to become an alert, the trace says whether it was DynamoDB, the engine or SNS. |
| **SSM Parameter Store** | Publishes the API endpoint at `/aquasentinel/<site>/api-endpoint` | Devices and scripts would need the endpoint hardcoded or passed by hand. |
| **IAM** | 23 resources: one least-privilege role per function | — |

## How a reading becomes an alert

1. The node publishes to `aquasentinel/telemetry/<device_id>` over MQTT with
   its own X.509 certificate, QoS 1.
2. Two IoT topic rules fire on the same message. One invokes **ingest**; the
   other writes to **Firehose** for the lake. They are independent, so an
   archive problem cannot stop detection.
3. **ingest** loads the device's engine state from **DynamoDB**, runs the four
   modules, writes back the state, the reading and any alerts.
4. A newly raised alert goes to **SNS** (notification) and to the
   **EventBridge** bus (an `alert.<module>.<severity>` event).
5. Every alert event triggers **explain**, which asks **Bedrock** for two plain
   sentences and writes them onto the alert.
6. A `CRITICAL` event additionally starts the **Step Functions** escalation.
7. The dashboard on **CloudFront** polls the **API Gateway** endpoint, which
   invokes **api**, which reads **DynamoDB**.
8. Every five minutes, **EventBridge** runs **sweep**, which raises an alert for
   any node that has gone quiet.

## Cost shape

Every component is on-demand or free-tier-shaped. For a two-node site at a
15-second cadence (~11,500 messages/day):

- DynamoDB on-demand, Lambda, SNS and IoT Core messages all sit inside or near
  the free tier at this volume;
- Firehose and S3 are billed on GB, and the payload is a few hundred bytes
  GZIPped;
- CloudFront's price class is restricted to the cheapest regions;
- the readings table expires itself after 30 days by TTL;
- lake objects move to Infrequent Access after 60 days.

The two things to watch if the fleet grows are the 5-minute sweep (constant,
independent of fleet size) and CloudWatch log retention (set to two weeks).

`make destroy` removes everything; the buckets are `auto_delete_objects`.

## Verifying it for the submission

```bash
make synth                                   # renders CloudFormation, no deploy
make deploy ALERT_EMAIL=you@example.com      # then confirm the SNS subscription
./scripts/provision_device.sh                # issues a device certificate
source .env.iot && python -m sim.run --transport iot --scenario demo --speed 60
```

Then, for the video: **IoT Core → MQTT test client**, subscribe to
`aquasentinel/telemetry/#`, and show real messages arriving — followed by the
same reading appearing in DynamoDB and on the CloudFront dashboard.
