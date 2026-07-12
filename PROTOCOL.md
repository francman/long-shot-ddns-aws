# long-shot-ddns wire protocol

Single source of truth for the contract between the Pi client
([francman/long-shot-ddns](https://github.com/francman/long-shot-ddns) —
the Raspberry Pi side) and this AWS Lambda backend.

When this file changes, both repos need a matching update.

## Endpoint

- Method: `POST`
- Path: `/update`
- Full URL after deploy: `https://<your-custom-domain>/update` (e.g. `https://ddns.example.com/update`)
  - Fallback / default: `https://<api-id>.execute-api.<region>.amazonaws.com/prod/update`
- Content-Type: `application/json`
- Authentication: API Gateway API key in the `x-api-key` header. The Lambda doesn't see the key — API Gateway rejects unauthenticated requests upstream with `403 Forbidden`.

## Request body

```json
{
  "hostname": "home.example.com",
  "ip": "203.0.113.42",
  "pi_id": "32-hex-char-machine-id"
}
```

- `hostname`: fully-qualified DNS name to update. Must be a **strict subdomain** of the hosted zone the Lambda is configured for; the Lambda rejects anything else with `403`. The zone apex and the API's own custom domain (e.g. `ddns.example.com`) are reserved infrastructure records and are also rejected with `403`.
- `ip`: IPv4 address the Pi observed for itself. Required. IPv6 / AAAA records are not supported in v1.
- `pi_id`: stable per-device identifier. The Pi reads `/etc/machine-id` (32 hex chars, set at first boot on every modern Linux distribution). Required.

## Ownership semantics (DynamoDB-backed)

The Lambda maintains a `LongShotDdnsOwnership` table keyed on `hostname`.

- **First** POST for a hostname: Lambda does a conditional `PutItem` that succeeds only if the hostname has no existing row. That Pi now owns the hostname.
- **Subsequent** POSTs from the **same** `pi_id`: Lambda confirms ownership via a `GetItem` and proceeds. No DynamoDB write.
- POSTs from a **different** `pi_id` for an already-claimed hostname: `409 Conflict`, no DNS change.

To release a hostname (e.g. you scrapped a Pi and want a new one to claim it):

```sh
aws dynamodb delete-item --table-name LongShotDdnsOwnership \
  --key '{"hostname":{"S":"home.example.com"}}'
```

The next Pi to POST that hostname will claim it.

## Route 53 semantics (idempotent)

The Lambda reads the current value of the A record before writing:

| Current state | Action | `status` in response |
|---|---|---|
| Record doesn't exist | Create it | `created` |
| Record exists, IP differs | UPSERT to new IP | `updated` |
| Record exists, IP matches | No write | `unchanged` |

Steady-state heartbeats (Pi POSTs same IP every 24h) cost **zero** Route 53 writes.

## Response

| Status | Body | When |
|---|---|---|
| `200 OK` | `{"status": "created" \| "updated" \| "unchanged", "hostname", "ip", "ttl"}` | Request accepted (with or without an actual Route 53 write) |
| `400 Bad Request` | `{"error": "..."}` | Malformed JSON, missing/invalid `hostname`, `ip`, or `pi_id` |
| `403 Forbidden` | (from API Gateway) default error JSON | Missing or invalid API key |
| `403 Forbidden` | `{"error": "..."}` | (from Lambda) `hostname` outside the configured zone, or a reserved hostname (zone apex / the API's own domain) |
| `409 Conflict` | `{"error": "..."}` | `hostname` already claimed by a different `pi_id` |
| `5xx` | Lambda/API Gateway default error JSON | Route 53 / DynamoDB / Lambda failure |

The client logs the response body but doesn't parse it. It retries on the next 5-minute timer tick — no in-process exponential backoff.

## Versioning

Currently un-versioned. If the contract ever needs an incompatible change, both sides will adopt an `x-protocol-version` request header.
