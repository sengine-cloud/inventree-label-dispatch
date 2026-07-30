"""Turning what the printer reported into what the Machines page should say.

Kept apart from :mod:`driver` and free of any InvenTree import on purpose. The driver
is thin by design and cannot be unit-tested here -- InvenTree is not a dependency of
this package's test environment -- so the decisions worth testing live here and return
a ``LabelPrinterStatus`` *member name* for the driver to look up.

Two rules the mapping is built around:

* **Silence is not health.** ``media_ok`` is ``None`` when the printer has not said
  anything about its media, which is a different state from "media is fine". Reporting
  the former as CONNECTED-and-fine is how a Bluetooth printer with no tape in it ends
  up looking ready.
* **A media fault is not a generic error.** InvenTree has NO_MEDIA (301) for exactly
  this, and it renders differently from ERROR (500). Using the specific code is the
  difference between "go put tape in it" and "something is wrong, go investigate".
"""

from __future__ import annotations

#: Terminal job states that mean tape came out as intended.
_GOOD_JOB_STATES = frozenset({"completed"})


def _describe(status: dict) -> str:
    """A one-line summary of the device truth on the retained status topic."""
    bits: list[str] = []
    if firmware := status.get("firmware"):
        bits.append(f"fw {firmware}")

    battery, voltage = status.get("battery_pct"), status.get("voltage_v")
    if battery is not None and voltage is not None:
        # Both, because the percentage pins at 100 on charge and the voltage does not.
        bits.append(f"{battery}% ({voltage:.2f}V)")
    elif battery is not None:
        bits.append(f"{battery}%")
    elif voltage is not None:
        bits.append(f"{voltage:.2f}V")

    media = status.get("media_ok")
    bits.append("media ok" if media else "no media" if media is False else "media unreported")

    if serial := status.get("serial"):
        bits.append(serial)
    return " · ".join(bits)


def classify_status(status: dict | None) -> tuple[str, str]:
    """Map a retained ``PrinterStatus`` to ``(LabelPrinterStatus member, text)``.

    ``status`` is ``None`` when nothing is retained on the topic -- the agent has never
    run, or its status was cleared. That is genuinely unknown, not disconnected.
    """
    if status is None:
        return "UNKNOWN", "no retained status; the print agent has not published yet"

    state = status.get("state")
    detail = _describe(status)

    if state == "disconnected":
        # The agent's MQTT will, or its own shutdown notice. The broker is reachable;
        # the printer is not.
        return "DISCONNECTED", f"agent reports the printer unreachable — {detail}"

    if state == "error":
        reason = status.get("error") or "printer reported a fault"
        # NO_MEDIA only when the printer actually said the media is bad. Any other
        # fault -- material error, a cancelled print -- is a generic error.
        member = "NO_MEDIA" if status.get("media_ok") is False else "ERROR"
        return member, f"{reason} — {detail}"

    if state == "printing":
        pending = status.get("pending_labels") or 0
        suffix = f", {pending} label(s) pending" if pending else ""
        return "PRINTING", f"printing{suffix} — {detail}"

    if state == "idle":
        # Idle but the media bit is clear: the agent has not faulted the batch (it may
        # not have printed since), yet the printer is not ready. Say so.
        if status.get("media_ok") is False:
            return "NO_MEDIA", f"printer reports no media — {detail}"
        return "CONNECTED", detail

    return "UNKNOWN", f"unrecognised agent state {state!r} — {detail}"


def classify_result(result: dict | None, *, dispatched: int) -> tuple[str, str]:
    """Map a ``JobResult`` to ``(LabelPrinterStatus member, text)``.

    ``None`` means no result arrived inside the wait. That is not a failure: strip mode
    deliberately holds labels until the coalescer flushes, so a job can legitimately
    outlive any sane wait. Leave it PRINTING rather than inventing an outcome.
    """
    if result is None:
        return "PRINTING", f"dispatched {dispatched} label(s); awaiting confirmation"

    state = result.get("state")
    labels = result.get("labels") or []
    printed = sum(label.get("copies_done", 0) for label in labels)

    if state in _GOOD_JOB_STATES:
        return "CONNECTED", f"printed {printed or dispatched} label(s)"

    if state == "partial":
        # Terminal, not in-flight: the agent sets this from `any_printed and any_failed`
        # when it finalises the job. So some tape came out and some labels did not, and
        # the operator needs the counts to know what to reprint -- "job partial" on its
        # own tells them nothing.
        failed = sum(1 for label in labels if label.get("state") == "failed")
        detail = result.get("error")
        member = "ERROR"
        text = f"printed {printed} of {len(labels)} label(s), {failed} failed"
        if detail:
            text = f"{text} — {detail}"
    elif state == "stalled":
        member = "DISCONNECTED"
        text = f"printer unreachable, job requeued — {result.get('error') or 'job stalled'}"
    else:
        member = "ERROR"
        text = result.get("error") or f"job {state}"

    # Appended after the branches, not inside one: every non-success outcome can have
    # moved tape, and a blind reprint after that double-prints whatever came out.
    if result.get("partial_tape_consumed"):
        text = f"{text} (tape partially consumed — check before reprinting)"
    return member, text
