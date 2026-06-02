# long-shot-ddns-aws

AWS-side of the **long-shot-ddns** project: a Lambda + API Gateway that receives `{hostname, ip, pi_id}` from a Raspberry Pi and upserts a Route 53 A record, with DynamoDB-backed hostname ownership and idempotent writes.

The matching Pi-side client: [francman/long-shot-ddns](https://github.com/francman/long-shot-ddns).

See [`PROTOCOL.md`](PROTOCOL.md) for the wire contract shared with the client and [`SECURITY.md`](SECURITY.md) for the threat model.

## What it provisions

- **Lambda function** (Python 3.12) — validates `{hostname, ip, pi_id}`, enforces ownership, idempotent Route 53 write.
- **API Gateway REST API** — `POST /update`, API-key auth, 10 req/s throttle + 20 burst.
- **Custom domain** (e.g. `ddns.example.com`) — ACM cert (DNS-validated) + `apigateway.DomainName` + Route 53 A-alias.
- **DynamoDB table** `LongShotDdnsOwnership` — write-once first-claim ownership keyed on hostname.
- **IAM role** scoped to one hosted zone (least privilege).
- **CloudWatch log group** with 30-day retention.

## Deploying your own

This repo is a template — every deployment owns its own AWS account, hosted zone, and custom domain. The deploy-time values live in `cdk.context.json` (gitignored), so each user fills in their own without modifying tracked code.

Prerequisites on the deployer's machine:
- Python 3.9+
- Node.js (for the `cdk` CLI binary)
- AWS CLI configured with credentials that can create Lambda / API Gateway / IAM / DynamoDB / ACM (`aws configure`)
- A Route 53 hosted zone for the domain you want to use (note its **hosted zone ID** — see Route 53 console or `aws route53 list-hosted-zones`)
- CDK CLI: `npm install -g aws-cdk`

```sh
git clone https://github.com/<your-fork>/long-shot-ddns-aws.git
cd long-shot-ddns-aws
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cat > cdk.context.json <<'EOF'
{
  "hosted_zone_id":   "Z123ABC456DEFGHIJKLMN",
  "hosted_zone_name": "example.com",
  "custom_domain":    "ddns.example.com",
  "record_ttl":       300
}
EOF

cdk bootstrap        # one-time per account/region
cdk deploy
```

ACM certificate validation runs as part of the deploy and typically adds 2-5 minutes the first time (DNS-validated against your hosted zone, automatically).

## Wire the Pi to the deployed backend

After `cdk deploy` completes, the outputs include the public endpoint URL and the API key ID. Fetch the actual API key value:

```sh
aws apigateway get-api-key \
  --api-key <ApiKeyId from cdk output> \
  --include-value --query value --output text
```

Then on the Pi:

```sh
sudo /opt/long-shot-ddns/client/install.sh --reconfigure
# Endpoint: <EndpointUrl from cdk output, e.g. https://ddns.example.com/update>
# Hostname: home.example.com (or whichever record under your zone)
# API key:  <value you just fetched>

sudo systemctl enable --now long-shot-ddns.timer
journalctl -u long-shot-ddns.service -f
```

## Verify end-to-end

```sh
# From your laptop:
curl -X POST https://<your-custom-domain>/update \
     -H "Content-Type: application/json" \
     -H "x-api-key: <key>" \
     -d '{"hostname":"home.example.com","ip":"203.0.113.42","pi_id":"<32 hex chars>"}'
# Expect: {"status":"created"|"updated"|"unchanged","hostname":"home.example.com","ip":"203.0.113.42","ttl":300}

dig +short home.example.com
# Expect: 203.0.113.42 (within a few seconds of the UPSERT)
```

## Administration

Release a hostname (so a new Pi can claim it):

```sh
aws dynamodb delete-item --table-name LongShotDdnsOwnership \
  --key '{"hostname":{"S":"home.example.com"}}'
```

Inspect ownership:

```sh
aws dynamodb scan --table-name LongShotDdnsOwnership --output table
```

Rotate the API key (delete + re-deploy generates a new one):

```sh
# Delete the existing key via console or CLI, then:
cdk deploy
# Fetch the new key value and reconfigure the Pi.
```

## Layout

```
long-shot-ddns-aws/
├── PROTOCOL.md            ← wire contract shared with the client
├── SECURITY.md            ← threat model
├── cdk.json
├── requirements.txt       ← aws-cdk-lib, constructs, boto3 (for local handler tests)
├── app.py                 ← CDK entry point (reads context values)
├── stacks/ddns_stack.py   ← Lambda + API Gateway + DynamoDB + custom domain + IAM
└── src/handler.py         ← the Lambda code
```

## Teardown

```sh
cdk destroy
```

Removes the Lambda, API Gateway, DynamoDB table, ACM cert, custom domain, alias record, and IAM role. The Route 53 hosted zone itself (created separately) is untouched.

## Costs (rough, for a single Pi at the default 5-minute cadence)

| Resource | Monthly cost |
|---|---|
| Lambda | $0 (free tier covers ~12k invocations/month; we use ~50) |
| API Gateway REST | ~$0.03 (~50 requests/mo; free first 12 months on new accounts) |
| DynamoDB | $0 (well under always-free tier of 25 RCU + 25 WCU) |
| CloudWatch Logs | ~$0.03 (~50 MB/mo at 30-day retention) |
| ACM cert | $0 |
| Custom domain on API GW | $0 |
| Route 53 hosted zone | $0.50 (one-time per zone, you already pay this if you own the domain) |

Total new recurring: **~$0.10/month**. Including the hosted zone: **~$0.60/month**.
