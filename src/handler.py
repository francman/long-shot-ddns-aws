"""long-shot-ddns Lambda handler.

Receives {hostname, ip, pi_id} from the Pi client (authenticated upstream by
API Gateway via an API key) and:

  1. Verifies ownership of the hostname in DynamoDB:
       - first POST for hostname  -> claim it (conditional PutItem)
       - subsequent POSTs from the same pi_id -> read-only verify
       - POST from a different pi_id -> 409 Conflict
  2. Reads the current Route 53 A record (if any).
  3. UPSERTs only when the IP actually differs.

Environment:
  HOSTED_ZONE_ID  — Route 53 hosted zone ID (required, set by CDK)
  RECORD_TTL      — A-record TTL in seconds (default 300, set by CDK)
  OWNERSHIP_TABLE — DynamoDB table name (required, set by CDK)

Wire contract: see PROTOCOL.md.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HOSTED_ZONE_ID = os.environ["HOSTED_ZONE_ID"]
RECORD_TTL = int(os.environ.get("RECORD_TTL", "300"))
OWNERSHIP_TABLE = os.environ["OWNERSHIP_TABLE"]

route53 = boto3.client("route53")
ownership = boto3.resource("dynamodb").Table(OWNERSHIP_TABLE)

# RFC 1035-ish hostname pattern.
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)
# /etc/machine-id is 32 hex chars on Linux.
PI_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def _claim_or_verify(hostname: str, pi_id: str) -> tuple[bool, Optional[str]]:
    """Return (allowed, error_message).

    Tries to claim the hostname for this pi_id via a conditional PutItem.
    If the row already exists, fall back to a GetItem to check whether the
    existing owner matches. Never overwrites an existing claim.
    """
    import time

    try:
        ownership.put_item(
            Item={"hostname": hostname, "pi_id": pi_id, "claimed_at": int(time.time())},
            ConditionExpression="attribute_not_exists(hostname)",
        )
        logger.info("claimed %s for pi_id=%s", hostname, pi_id)
        return True, None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # Row exists — check the existing owner.

    existing = ownership.get_item(Key={"hostname": hostname}).get("Item", {})
    existing_pi_id = existing.get("pi_id")
    if existing_pi_id == pi_id:
        return True, None
    logger.warning(
        "claim conflict: hostname=%s owned by pi_id=%s..., rejected pi_id=%s...",
        hostname, str(existing_pi_id)[:8], pi_id[:8],
    )
    return False, (
        f"hostname '{hostname}' is already claimed by a different device. "
        "Delete the DynamoDB row to release it."
    )


def _current_record_ip(hostname: str) -> Optional[str]:
    """Return the IP currently published for hostname, or None if no A record."""
    resp = route53.list_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        StartRecordName=hostname + ".",
        StartRecordType="A",
        MaxItems="1",
    )
    for rrset in resp.get("ResourceRecordSets", []):
        if rrset["Name"].rstrip(".") == hostname and rrset["Type"] == "A":
            records = rrset.get("ResourceRecords", [])
            if records:
                return records[0]["Value"]
    return None


def _upsert_a_record(hostname: str, ip: str) -> None:
    route53.change_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        ChangeBatch={
            "Comment": "long-shot-ddns update",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": hostname + ".",
                        "Type": "A",
                        "TTL": RECORD_TTL,
                        "ResourceRecords": [{"Value": ip}],
                    },
                }
            ],
        },
    )


def handler(event: dict, _context: Any) -> dict:
    try:
        payload = _parse_body(event)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("invalid JSON body: %s", exc)
        return _response(400, {"error": "request body must be valid JSON"})

    hostname = (payload.get("hostname") or "").strip().lower().rstrip(".")
    ip = (payload.get("ip") or "").strip()
    pi_id = (payload.get("pi_id") or "").strip().lower()

    if not hostname or not HOSTNAME_RE.match(hostname):
        return _response(400, {"error": "missing or invalid 'hostname'"})
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ValueError, ipaddress.AddressValueError):
        return _response(400, {"error": "missing or invalid 'ip' (IPv4 required)"})
    if not PI_ID_RE.match(pi_id):
        return _response(
            400,
            {"error": "missing or invalid 'pi_id' (expected 32 hex chars, /etc/machine-id format)"},
        )

    # Ownership gate.
    allowed, err = _claim_or_verify(hostname, pi_id)
    if not allowed:
        return _response(409, {"error": err})

    # Idempotent Route 53 write — read current value, only UPSERT on change.
    current_ip = _current_record_ip(hostname)
    if current_ip == str(addr):
        logger.info("no-op: %s already points at %s", hostname, addr)
        return _response(
            200,
            {"status": "unchanged", "hostname": hostname, "ip": str(addr), "ttl": RECORD_TTL},
        )

    if current_ip is None:
        logger.info("creating A %s -> %s (zone %s, ttl %s)", hostname, addr, HOSTED_ZONE_ID, RECORD_TTL)
        status = "created"
    else:
        logger.info("updating A %s: %s -> %s (zone %s, ttl %s)", hostname, current_ip, addr, HOSTED_ZONE_ID, RECORD_TTL)
        status = "updated"

    _upsert_a_record(hostname, str(addr))

    return _response(
        200,
        {"status": status, "hostname": hostname, "ip": str(addr), "ttl": RECORD_TTL},
    )
