"""The pure job-building and publish path -- no InvenTree, no broker."""

from __future__ import annotations

import json

from inventree_label_dispatch import dispatch
from inventree_label_dispatch.dispatch import Connection, build_job, build_label, publish


def test_build_label_preset_and_elements():
    preset = build_label({"preset": "stock_item", "vars": {"code": "SI1"}}, copies=2, dedupe_key="k")
    assert preset["copies"] == 2 and preset["dedupe_key"] == "k"
    assert preset["preset"] == "stock_item"

    raw = build_label({"elements": [{"type": "raw_png", "data_b64": "x"}]}, length_mm=25)
    assert raw["length_mm"] == 25 and "preset" not in raw


def test_build_job_is_contract_v1():
    job = build_job(
        "d30-workshop",
        [build_label({"preset": "stock_item", "vars": {"code": "SI1"}})],
        tape_width_mm=15.0,
        template_id=7,
    )
    assert job["v"] == 1
    assert job["printer"] == {"id": "d30-workshop", "require_model": "phomemo-d30"}
    assert job["tape"]["width_mm"] == 15.0
    assert job["options"]["batch_mode"] == "strip"
    # Every click is a distinct delivery: job_id is embedded in the idempotency key.
    assert job["job_id"] in job["idempotency_key"]
    assert job["idempotency_key"].startswith("inventree:tpl7:")


def test_two_jobs_have_distinct_ids():
    a = build_job("d30-workshop", [build_label({"preset": "p", "vars": {}})])
    b = build_job("d30-workshop", [build_label({"preset": "p", "vars": {}})])
    assert a["job_id"] != b["job_id"]


class _FakeInfo:
    def wait_for_publish(self, timeout=None):
        return True


class _FakeClient:
    def __init__(self):
        self.published = []
        self.connected = False

    def connect(self, host, port, keepalive):
        self.connected = True

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.connected = False

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return _FakeInfo()


def test_publish_sends_job_to_the_printers_jobs_topic(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)
    job = build_job("d30-workshop", [build_label({"preset": "stock_item", "vars": {"code": "SI1"}})])

    publish(job, Connection(host="broker", topic_prefix="se/v1/print"))

    assert len(fake.published) == 1
    topic, payload, qos = fake.published[0]
    assert topic == "se/v1/print/d30-workshop/jobs"
    assert qos == 1
    assert json.loads(payload)["job_id"] == job["job_id"]
