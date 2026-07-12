"""Unit tests for the Lambda handler: validation order, zone/reserved gates,
ownership semantics, idempotent Route 53 writes.

Run from the repo root:  pip install pytest && pytest
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PI_A = "a" * 32
PI_B = "b" * 32


class FakeTable:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def put_item(self, Item, ConditionExpression=None):
        self.calls.append("put_item")
        if Item["hostname"] in self.rows:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.rows[Item["hostname"]] = Item

    def get_item(self, Key):
        self.calls.append("get_item")
        item = self.rows.get(Key["hostname"])
        return {"Item": item} if item else {}


class FakeRoute53:
    def __init__(self):
        self.records = {}
        self.calls = []

    def list_resource_record_sets(self, **kw):
        self.calls.append("list")
        name = kw["StartRecordName"]
        if name in self.records:
            return {
                "ResourceRecordSets": [
                    {
                        "Name": name,
                        "Type": "A",
                        "ResourceRecords": [{"Value": self.records[name]}],
                    }
                ]
            }
        return {"ResourceRecordSets": []}

    def change_resource_record_sets(self, **kw):
        self.calls.append("upsert")
        rr = kw["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
        self.records[rr["Name"]] = rr["ResourceRecords"][0]["Value"]


@pytest.fixture()
def env(monkeypatch):
    """Fresh handler module wired to fakes; returns (module, table, route53)."""
    monkeypatch.setenv("HOSTED_ZONE_ID", "Z_TEST")
    monkeypatch.setenv("HOSTED_ZONE_NAME", "example.com")
    monkeypatch.setenv("RECORD_TTL", "300")
    monkeypatch.setenv("OWNERSHIP_TABLE", "TestTable")
    monkeypatch.setenv("RESERVED_HOSTNAMES", "ddns.example.com")

    table = FakeTable()
    r53 = FakeRoute53()

    class FakeDdb:
        def Table(self, name):
            return table

    monkeypatch.setattr(boto3, "client", lambda name: r53)
    monkeypatch.setattr(boto3, "resource", lambda name: FakeDdb())

    sys.modules.pop("handler", None)
    handler = importlib.import_module("handler")
    yield handler, table, r53
    sys.modules.pop("handler", None)


def post(handler, body) -> tuple[int, dict]:
    raw = body if isinstance(body, str) else json.dumps(body)
    resp = handler.handler({"body": raw}, None)
    return resp["statusCode"], json.loads(resp["body"])


def valid(**overrides):
    body = {"hostname": "home.example.com", "ip": "203.0.113.42", "pi_id": PI_A}
    body.update(overrides)
    return body


# --- 400s: malformed input ---------------------------------------------------

def test_rejects_bad_json(env):
    handler, _, _ = env
    status, body = post(handler, "{nope")
    assert status == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostname": ""},
        {"hostname": "not_a_hostname!"},
        {"ip": "999.1.1.1"},
        {"ip": ""},
        {"pi_id": "xyz"},
        {"pi_id": ""},
    ],
)
def test_rejects_invalid_fields(env, overrides):
    handler, table, r53 = env
    status, _ = post(handler, valid(**overrides))
    assert status == 400
    assert table.calls == [] and r53.calls == []


# --- 403s: zone and reserved gates (before any AWS call) ----------------------

@pytest.mark.parametrize(
    "hostname,expected_error",
    [
        ("home.other.com", "not a subdomain"),
        ("evilexample.com", "not a subdomain"),   # suffix trick
        ("example.com", "reserved"),               # zone apex
        ("ddns.example.com", "reserved"),          # the API's own domain
        ("DDNS.EXAMPLE.COM.", "reserved"),         # case + trailing-dot normalization
    ],
)
def test_gates_reject_with_no_side_effects(env, hostname, expected_error):
    handler, table, r53 = env
    status, body = post(handler, valid(hostname=hostname))
    assert status == 403
    assert expected_error in body["error"]
    assert table.calls == [] and r53.calls == []


# --- ownership + idempotent writes ---------------------------------------------

def test_first_claim_creates_record(env):
    handler, table, r53 = env
    status, body = post(handler, valid())
    assert (status, body["status"]) == (200, "created")
    assert table.rows["home.example.com"]["pi_id"] == PI_A
    assert r53.records["home.example.com."] == "203.0.113.42"


def test_same_pi_same_ip_is_noop(env):
    handler, table, r53 = env
    post(handler, valid())
    r53.calls.clear()
    status, body = post(handler, valid())
    assert (status, body["status"]) == (200, "unchanged")
    assert "upsert" not in r53.calls


def test_same_pi_new_ip_updates(env):
    handler, _, r53 = env
    post(handler, valid())
    status, body = post(handler, valid(ip="203.0.113.99"))
    assert (status, body["status"]) == (200, "updated")
    assert r53.records["home.example.com."] == "203.0.113.99"


def test_different_pi_conflicts(env):
    handler, table, r53 = env
    post(handler, valid())
    r53.calls.clear()
    status, body = post(handler, valid(pi_id=PI_B, ip="9.9.9.9"))
    assert status == 409
    assert "already claimed" in body["error"]
    assert "upsert" not in r53.calls
    assert table.rows["home.example.com"]["pi_id"] == PI_A


def test_base64_body(env):
    handler, _, _ = env
    import base64

    raw = base64.b64encode(json.dumps(valid()).encode()).decode()
    resp = handler.handler({"body": raw, "isBase64Encoded": True}, None)
    assert resp["statusCode"] == 200
