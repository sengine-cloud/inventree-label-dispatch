"""Building and publishing a labelfab job. No InvenTree imports live here.

This is the whole reason the plugin is testable: turning items into a v1 job dict
and pushing it to MQTT is pure Python, so it is unit-tested directly, and the driver
that wires it into InvenTree stays a thin adapter.

A short-lived connection per print (connect -> publish QoS 1 -> disconnect) is
deliberate: Django-Q workers are forked, and a persistent paho client held across a
fork is a reliable source of silently-vanishing publishes.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from .status import parse_seen_at

#: Mirrors labelfab's contract v1. Kept as a plain dict builder rather than importing
#: labelfab so the InvenTree image does not pull in Pillow/qrcode a second time.
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class Connection:
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    transport: str = "tcp"  # tcp | websockets
    ws_path: str = "/mqtt"
    topic_prefix: str = "se/v1/print"
    keepalive_s: int = 30
    timeout_s: float = 5.0

    def topic(self, printer_id: str, leaf: str) -> str:
        return f"{self.topic_prefix}/{printer_id}/{leaf}"


def _new_client(conn: Connection, client_suffix: str):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"inventree-{client_suffix}-{uuid.uuid4().hex[:8]}",
        transport="websockets" if conn.transport == "websockets" else "tcp",
    )
    if conn.transport == "websockets":
        client.ws_set_options(path=conn.ws_path)
    if conn.tls:
        client.tls_set()
    if conn.username:
        client.username_pw_set(conn.username, conn.password)
    return client


def build_label(spec: dict, *, copies: int = 1, length_mm=None, dedupe_key: str | None = None) -> dict:
    """One label entry. ``spec`` is either ``{"preset","vars"}`` or ``{"elements":[...]}``."""
    label: dict = dict(spec)
    label["copies"] = copies
    if length_mm is not None:
        label["length_mm"] = length_mm
    if dedupe_key:
        label["dedupe_key"] = dedupe_key
    return label


def build_job(
    printer_id: str,
    labels: list[dict],
    *,
    tape_width_mm: float = 15.0,
    tape_kind: str = "continuous",
    tape_length_mm="auto",
    batch_mode: str = "strip",
    template_id: int | None = None,
) -> dict:
    """Assemble a contract-v1 job. The ULID makes every UI click a distinct delivery,
    so dedupe protects against transport retries, not a deliberate reprint."""
    job_ulid = uuid.uuid4().hex
    key_scope = f"inventree:tpl{template_id}:" if template_id is not None else "inventree:"
    return {
        "v": CONTRACT_VERSION,
        "job_id": job_ulid,
        "idempotency_key": f"{key_scope}{job_ulid}",
        "printer": {"id": printer_id, "require_model": "phomemo-d30"},
        "tape": {"width_mm": tape_width_mm, "length_mm": tape_length_mm, "kind": tape_kind},
        "options": {"batch_mode": batch_mode},
        "labels": labels,
    }


def publish(job: dict, conn: Connection) -> None:
    """Publish one job with a short-lived, forked-worker-safe connection."""
    client = _new_client(conn, "publish")
    client.connect(conn.host, conn.port, conn.keepalive_s)
    client.loop_start()
    try:
        info = client.publish(conn.topic(job["printer"]["id"], "jobs"), json.dumps(job), qos=1)
        info.wait_for_publish(conn.timeout_s)
    finally:
        client.loop_stop()
        client.disconnect()


def publish_awaiting_result(job: dict, conn: Connection, timeout_s: float) -> dict | None:
    """Publish a job and wait for *its* ``JobResult``, or ``None`` on timeout.

    Subscribes before publishing. The results topic is **not** retained, so subscribing
    afterwards races the agent and would lose the result of a fast print entirely.

    Filters on ``job_id``: one printer serves every InvenTree user, so another click's
    result can land on this subscription and must not be mistaken for ours.

    A timeout is an ordinary outcome, not an error. Strip mode holds labels until the
    coalescer flushes -- up to ``strip.max_wait_s`` on the agent, 30s by default -- so a
    job can legitimately outlive any wait short enough to block a worker on.
    """
    import queue

    printer_id = job["printer"]["id"]
    job_id = job["job_id"]
    received: queue.Queue = queue.Queue()

    def _on_message(_c, _u, msg):
        try:
            payload = json.loads(msg.payload)
        except Exception:
            return
        if payload.get("job_id") == job_id:
            received.put_nowait(payload)

    client = _new_client(conn, "job")
    client.on_message = _on_message
    client.connect(conn.host, conn.port, conn.keepalive_s)
    client.subscribe(conn.topic(printer_id, "results"), qos=1)
    client.loop_start()
    try:
        info = client.publish(conn.topic(printer_id, "jobs"), json.dumps(job), qos=1)
        info.wait_for_publish(conn.timeout_s)
        try:
            return received.get(timeout=timeout_s)
        except queue.Empty:
            return None
    finally:
        client.loop_stop()
        client.disconnect()


def probe_status(printer_id: str, conn: Connection, wait_s: float = 8.0) -> dict | None:
    """Ask the agent to look at the printer, and return the first *newer* reading.

    ``read_status`` returns what the agent *remembers*, which is the right default:
    the D30 is only reachable while it is being printed to, so remembered truth is
    almost always the best truth available and costs nothing. This is the other case --
    a human pressed refresh and wants the printer asked, not the memory read.

    "Newer" is load-bearing and means a ``device_seen_at`` later than the one already
    retained, not merely the next message to arrive. The agent publishes on its own
    events too, so without that test a print starting, a broker reconnect or somebody
    else pressing the same button could be returned as the answer while the real one
    was thrown away.

    Subscribes *before* publishing the command, because the answer is a retained
    message and a subscription set up afterwards would race the agent. The retained
    message already on the topic arrives immediately and is kept as the baseline, so a
    probe the agent cannot fulfil (the printer is asleep, which is normal) degrades to
    exactly what ``read_status`` would have returned rather than to nothing -- or to
    something newer, if one of those other events arrived while we waited.

    ``wait_s`` covers the agent's round trip -- connect, session setup, telemetry,
    publish -- which is a few seconds when the printer answers and one connect timeout
    when it does not.

    It is not the whole bound, though, and anyone sizing a request timeout around this
    should use ``2 * conn.timeout_s + wait_s``: reading the retained message and waiting
    for the command's PUBACK can each cost ``conn.timeout_s`` first. Both are sub-
    millisecond against a healthy broker, and a broker slow enough for them to matter
    has already broken ``read_status`` on every other path. This blocks the request that
    triggered it either way, so all three are bounded rather than generous.
    """
    import queue

    received: queue.Queue = queue.Queue()
    client = _new_client(conn, "probe")
    client.on_message = lambda _c, _u, msg: received.put_nowait(msg.payload)
    client.connect(conn.host, conn.port, conn.keepalive_s)
    client.subscribe(conn.topic(printer_id, "status"), qos=1)
    client.loop_start()
    try:
        latest = None
        try:  # the retained message: what we already knew, before asking
            latest = json.loads(received.get(timeout=conn.timeout_s))
        except (queue.Empty, ValueError):
            pass

        # Everything queued before the command goes out is context, not answer: the
        # retained message plus anything the agent happened to publish while we were
        # connecting. Folding it into the baseline is what makes "newer than what we
        # knew" a property of this function rather than a bet on how the agent emits.
        # Otherwise, when nothing has ever been observed and the baseline is None, the
        # filter weakens to "the first message carrying any timestamp at all", and that
        # is only safe because a pre-observation agent publishes null everywhere -- an
        # invariant that lives in the other repo and could be relaxed there without
        # anyone here noticing.
        #
        # Drained before publishing rather than after, so a quick answer cannot be
        # swallowed into the baseline it is supposed to beat.
        while True:
            try:
                latest = json.loads(received.get_nowait())
            except queue.Empty:
                break
            except ValueError:
                continue

        info = client.publish(conn.topic(printer_id, "cmd"), "probe", qos=1)
        info.wait_for_publish(conn.timeout_s)

        # The answer is the first status carrying an observation *newer* than the one we
        # already had -- not simply the first status to arrive. The agent publishes on
        # its own events too (a print starting and finishing, a broker reconnect,
        # somebody else pressing this button), and any of those landing first would
        # otherwise be mistaken for the answer while the real one was discarded. The
        # symptom is mild and self-correcting, which is exactly why it would have been
        # hard to spot: `state=printing` on a page you just refreshed looks plausible.
        #
        # A status published by a print that *did* reach the printer also carries a
        # newer observation, and returning that is right -- it is the same freshness a
        # probe would have produced, obtained the same way.
        baseline = parse_seen_at((latest or {}).get("device_seen_at"))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                payload = received.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            seen = parse_seen_at(message.get("device_seen_at"))
            if seen is not None and (baseline is None or seen > baseline):
                return message
            # Not the answer, but still more current than what we started with: keep it
            # as the fallback, so a probe that goes unanswered reports the newest state
            # rather than the one that happened to be retained when we subscribed.
            latest = message
        return latest
    finally:
        client.loop_stop()
        client.disconnect()


def read_status(printer_id: str, conn: Connection) -> dict | None:
    """One-shot read of the retained status topic. ``None`` if nothing is retained.

    Retained state means the Machines UI shows connectivity with no polling loop in
    the Django worker: connect, grab the retained message, disconnect.
    """
    import queue

    received: queue.Queue = queue.Queue(maxsize=1)
    client = _new_client(conn, "status")

    def _on_message(_c, _u, msg):
        try:
            received.put_nowait(json.loads(msg.payload))
        except Exception:
            pass

    client.on_message = _on_message
    client.connect(conn.host, conn.port, conn.keepalive_s)
    client.subscribe(conn.topic(printer_id, "status"), qos=1)
    client.loop_start()
    try:
        return received.get(timeout=conn.timeout_s)
    except queue.Empty:
        return None
    finally:
        client.loop_stop()
        client.disconnect()
