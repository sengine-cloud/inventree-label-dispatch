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


class _ReplyingClient(_FakeClient):
    """A broker that answers each publish with results, in subscribe-then-publish order."""

    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.subscribed = []
        self.on_message = None
        self.order = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        self.order.append("subscribe")

    def publish(self, topic, payload, qos=0):
        self.order.append("publish")
        info = super().publish(topic, payload, qos)
        for reply in self.replies:
            self.on_message(self, None, type("Msg", (), {"payload": json.dumps(reply).encode()})())
        return info


def test_awaiting_result_subscribes_before_publishing(monkeypatch):
    """The results topic is not retained, so subscribing after publish loses fast prints."""
    job = build_job("d30-workshop", [build_label({"preset": "stock_item", "vars": {"code": "S"}})])
    fake = _ReplyingClient([{"job_id": job["job_id"], "state": "completed"}])
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    result = dispatch.publish_awaiting_result(job, Connection(host="broker"), timeout_s=1.0)

    assert fake.order == ["subscribe", "publish"]
    assert fake.subscribed == ["se/v1/print/d30-workshop/results"]
    assert result["state"] == "completed"


def test_awaiting_result_ignores_another_clicks_result(monkeypatch):
    """One printer serves every InvenTree user; a stranger's result is not ours."""
    job = build_job("d30-workshop", [build_label({"preset": "stock_item", "vars": {"code": "S"}})])
    fake = _ReplyingClient(
        [
            {"job_id": "someone-elses-job", "state": "failed", "error": "not ours"},
            {"job_id": job["job_id"], "state": "completed"},
        ]
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    result = dispatch.publish_awaiting_result(job, Connection(host="broker"), timeout_s=1.0)
    assert result["job_id"] == job["job_id"]
    assert result["state"] == "completed"


def test_awaiting_result_returns_none_on_timeout(monkeypatch):
    """A strip that has not flushed yet is an ordinary outcome, not an error."""
    job = build_job("d30-workshop", [build_label({"preset": "stock_item", "vars": {"code": "S"}})])
    fake = _ReplyingClient([])  # broker says nothing
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    assert dispatch.publish_awaiting_result(job, Connection(host="broker"), timeout_s=0.05) is None


def test_awaiting_result_survives_a_malformed_payload(monkeypatch):
    """A non-JSON message on the topic must not take down a print."""
    job = build_job("d30-workshop", [build_label({"preset": "stock_item", "vars": {"code": "S"}})])
    fake = _ReplyingClient([{"job_id": job["job_id"], "state": "completed"}])
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    original = fake.publish

    def publish_with_garbage(topic, payload, qos=0):
        fake.on_message(fake, None, type("Msg", (), {"payload": b"not json"})())
        return original(topic, payload, qos)

    fake.publish = publish_with_garbage
    result = dispatch.publish_awaiting_result(job, Connection(host="broker"), timeout_s=1.0)
    assert result["state"] == "completed"


# -- refresh: ask the printer, not the memory ------------------------------- #
#
# read_status returns what the agent remembers, which is the right default. probe_status
# is the other case: a human pressed refresh and wants the printer actually asked.


class _ProbeBroker(_FakeClient):
    """Retains one status, and optionally publishes more when probed.

    ``fresh`` may be a single status or a list, so a test can reproduce the agent
    publishing on its own events -- a print starting, a reconnect -- while a probe is
    still in flight.
    """

    def __init__(self, retained, fresh=None):
        super().__init__()
        self.retained = retained
        self.fresh = [] if fresh is None else (fresh if isinstance(fresh, list) else [fresh])
        self.subscribed = []
        self.on_message = None
        self.order = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        self.order.append("subscribe")
        if self.retained is not None:  # retained messages arrive on subscribe
            self._deliver(self.retained)

    def publish(self, topic, payload, qos=0):
        self.order.append("publish")
        info = super().publish(topic, payload, qos)
        for message in self.fresh:
            self._deliver(message)
        return info

    def _deliver(self, obj):
        payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.on_message(self, None, type("Msg", (), {"payload": payload})())


def test_probe_asks_the_agent_and_returns_the_fresh_answer(monkeypatch):
    fake = _ProbeBroker(
        retained={"state": "idle", "media_ok": True, "device_seen_at": "2026-08-01T00:00:00Z"},
        fresh={"state": "idle", "media_ok": True, "device_seen_at": "2026-08-02T12:00:00Z"},
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=1)

    assert got["device_seen_at"] == "2026-08-02T12:00:00Z"
    topic, payload, qos = fake.published[0]
    assert topic == "se/v1/print/d30-workshop/cmd"
    assert payload == "probe"
    assert qos == 1


def test_probe_subscribes_before_asking(monkeypatch):
    """The answer is a retained publish; subscribing afterwards races the agent."""
    fake = _ProbeBroker(retained={"state": "idle"}, fresh={"state": "idle"})
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=1)

    assert fake.order[:2] == ["subscribe", "publish"]


def test_a_printer_that_does_not_answer_falls_back_to_what_we_knew(monkeypatch):
    """The normal case for a D30 that has powered itself down. The reading keeps its
    old timestamp and the page says how old it is, rather than going blank."""
    fake = _ProbeBroker(
        retained={"state": "idle", "media_ok": True, "device_seen_at": "2026-08-01T00:00:00Z"},
        fresh=None,  # agent probed, printer was asleep, so it published nothing
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=0.3)

    assert got["device_seen_at"] == "2026-08-01T00:00:00Z"


def test_probe_with_nothing_retained_and_no_answer_is_none(monkeypatch):
    fake = _ProbeBroker(retained=None, fresh=None)
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)
    conn = Connection(host="broker", timeout_s=0.3)

    assert dispatch.probe_status("d30-workshop", conn, wait_s=0.3) is None


def test_probe_survives_a_malformed_retained_payload(monkeypatch):
    fake = _ProbeBroker(retained=b"{not json", fresh={"state": "idle", "media_ok": True})
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)
    conn = Connection(host="broker", timeout_s=0.3)

    got = dispatch.probe_status("d30-workshop", conn, wait_s=1)
    assert got == {"state": "idle", "media_ok": True}


def test_a_print_starting_mid_probe_does_not_win(monkeypatch):
    """The agent publishes on its own events -- a print starting, a broker reconnect,
    another click of the same button. Any of those landing first would otherwise be
    returned while the real answer was discarded, and `state=printing` on a page you
    just refreshed looks plausible enough that nobody would report it."""
    old = "2026-08-01T00:00:00Z"
    fake = _ProbeBroker(
        retained={"state": "idle", "media_ok": True, "device_seen_at": old},
        fresh=[
            # the in-flight print announcing itself: same observation, new state
            {"state": "printing", "pending_labels": 3, "device_seen_at": old},
            # the probe's actual answer
            {"state": "idle", "media_ok": True, "device_seen_at": "2026-08-02T12:00:00Z"},
        ],
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=1)

    assert got["state"] == "idle"
    assert got["device_seen_at"] == "2026-08-02T12:00:00Z"


def test_a_print_that_reached_the_printer_is_a_valid_answer(monkeypatch):
    """It carries a newer observation obtained the same way a probe would have."""
    fake = _ProbeBroker(
        retained={"state": "idle", "media_ok": True, "device_seen_at": "2026-08-01T00:00:00Z"},
        fresh=[{"state": "idle", "media_ok": False, "device_seen_at": "2026-08-02T09:00:00Z"}],
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=1)
    assert got["media_ok"] is False


def test_an_unanswered_probe_reports_the_newest_state_not_the_stale_one(monkeypatch):
    """If the printer never answers but the agent said something else meanwhile, that
    is still more current than what happened to be retained when we subscribed."""
    old = "2026-08-01T00:00:00Z"
    fake = _ProbeBroker(
        retained={"state": "idle", "device_seen_at": old},
        fresh=[{"state": "printing", "pending_labels": 2, "device_seen_at": old}],
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=0.3)
    assert got["state"] == "printing"


def test_an_identical_republish_is_not_an_answer(monkeypatch):
    """A broker reconnect republishes the same retained status verbatim."""
    same = {"state": "idle", "media_ok": True, "device_seen_at": "2026-08-01T00:00:00Z"}
    fake = _ProbeBroker(retained=same, fresh=[dict(same)])
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=0.3)
    assert got == same  # fell through to the fallback rather than claiming a fresh look


def test_sub_second_stamps_compare_chronologically_not_lexically(monkeypatch):
    """'...:42.5Z' sorts before '...:42Z' as a string, because '.' precedes 'Z'. Parsing
    is what keeps a newer reading from being discarded as older."""
    fake = _ProbeBroker(
        retained={"state": "idle", "device_seen_at": "2026-08-02T12:00:42Z"},
        fresh=[{"state": "idle", "media_ok": True, "device_seen_at": "2026-08-02T12:00:42.5Z"}],
    )
    monkeypatch.setattr(dispatch, "_new_client", lambda conn, suffix: fake)

    got = dispatch.probe_status("d30-workshop", Connection(host="broker"), wait_s=1)
    assert got["media_ok"] is True
