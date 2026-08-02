"""Mapping printer truth onto Machines-page status. No InvenTree, no broker.

The member names asserted here must exist on InvenTree's ``LabelPrinterStatus``:
CONNECTED 100, UNKNOWN 101, PRINTING 110, WARNING 200, NO_MEDIA 301, PAPER_JAM 302,
DISCONNECTED 400, ERROR 500.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from inventree_label_dispatch.status import classify_result, classify_status

VALID_MEMBERS = {
    "CONNECTED",
    "UNKNOWN",
    "PRINTING",
    "WARNING",
    "NO_MEDIA",
    "PAPER_JAM",
    "DISCONNECTED",
    "ERROR",
}


def _healthy(**over) -> dict:
    base = {
        "v": 1,
        "printer_id": "d30-workshop",
        "state": "idle",
        "model": "phomemo-d30",
        "serial": "Q223P4C31420105",
        "firmware": "2.1.2",
        "battery_pct": 100,
        "voltage_v": 4.17,
        "media_ok": True,
        "tape_width_mm": 15.0,
        "pending_labels": 0,
        "error": None,
    }
    base.update(over)
    return base


def test_no_mapping_returns_a_misspelled_member_name():
    """A typo becomes AttributeError inside a Django worker rather than failing here.

    This catches typos, not drift from InvenTree's enum: VALID_MEMBERS is a local copy,
    because InvenTree is deliberately not a test dependency of this package. Drift would
    have to be caught by the integration environment.
    """
    cases = [
        None,
        _healthy(),
        _healthy(state="printing", pending_labels=3),
        _healthy(state="disconnected"),
        _healthy(state="error", media_ok=False, error="media not ready"),
        _healthy(state="error", error="material error 0x02"),
        _healthy(state="banana"),
    ]
    for case in cases:
        member, text = classify_status(case)
        assert member in VALID_MEMBERS, member
        assert text


def test_idle_and_healthy_is_connected_with_the_device_detail():
    member, text = classify_status(_healthy())
    assert member == "CONNECTED"
    assert "fw 2.1.2" in text
    assert "100% (4.17V)" in text  # both, because percent pins at 100 on charge
    assert "media ok" in text


def test_no_retained_status_is_unknown_not_disconnected():
    """Nothing published is genuinely unknown; claiming disconnected invents a fact."""
    member, _text = classify_status(None)
    assert member == "UNKNOWN"


def test_a_media_fault_is_no_media_not_a_generic_error():
    """InvenTree renders NO_MEDIA differently: 'go load tape' vs 'go investigate'."""
    member, text = classify_status(
        _healthy(state="error", media_ok=False, error="media not ready (paper=ERROR(0x88))")
    )
    assert member == "NO_MEDIA"
    assert "media not ready" in text


def test_a_non_media_fault_is_a_generic_error():
    member, text = classify_status(_healthy(state="error", error="printer cancelled the print"))
    assert member == "ERROR"
    assert "cancelled" in text


def test_idle_but_media_bit_clear_still_reports_no_media():
    """The agent may not have printed since the tape ran out, so state is still idle."""
    member, _text = classify_status(_healthy(media_ok=False))
    assert member == "NO_MEDIA"


def test_unreported_media_is_not_shown_as_ok():
    """Silence is not health -- the whole reason media_ok is a tri-state."""
    _member, text = classify_status(_healthy(media_ok=None))
    assert "media unreported" in text
    assert "media ok" not in text


def test_printing_reports_the_pending_count():
    member, text = classify_status(_healthy(state="printing", pending_labels=4))
    assert member == "PRINTING"
    assert "4 label(s) pending" in text


def test_missing_optional_fields_do_not_break_the_description():
    """An older agent publishes v1 without the new fields."""
    member, text = classify_status(
        {"v": 1, "printer_id": "d30-workshop", "state": "idle", "model": "phomemo-d30"}
    )
    assert member == "CONNECTED"
    assert "media unreported" in text


# -- how old the device truth is -------------------------------------------- #
#
# The printer is only reachable while it is being printed to, so the agent republishes
# what it last heard rather than going quiet. That is the right trade, but it means
# "media ok" on this page is usually a memory, and a memory rendered without its age is
# indistinguishable from an observation.

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _seen(delta: timedelta) -> dict:
    return _healthy(device_seen_at=(NOW - delta).isoformat().replace("+00:00", "Z"))


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "seen just now"),
        (timedelta(seconds=9), "seen just now"),
        (timedelta(seconds=42), "seen 42s ago"),
        (timedelta(minutes=4), "seen 4m ago"),
        (timedelta(hours=6), "seen 6h ago"),
        (timedelta(days=3), "seen 3d ago"),
    ],
)
def test_the_age_of_the_device_truth_is_shown(delta, expected):
    _member, text = classify_status(_seen(delta), now=NOW)
    assert expected in text


def test_stale_truth_is_still_the_best_truth_available():
    """Age annotates the reading; it does not overrule it. A printer that had tape three
    days ago is far more likely to still have tape than to have anything else."""
    member, text = classify_status(_seen(timedelta(days=3)), now=NOW)
    assert member == "CONNECTED"
    assert "media ok" in text
    assert "seen 3d ago" in text


def test_an_agent_that_does_not_publish_the_field_renders_as_before():
    """Back-compat with an un-upgraded agent: no field, no invented age."""
    _member, text = classify_status(_healthy(), now=NOW)
    assert "seen" not in text
    assert "fw 2.1.2" in text


@pytest.mark.parametrize("value", [None, "", "not a timestamp", 12345, "2026-13-45T99:99:99Z"])
def test_an_unusable_timestamp_is_dropped_not_raised(value):
    """This runs in a Django worker on a payload from another process, so a surprise
    here is a 500 on the Machines page rather than a red test."""
    _member, text = classify_status(_healthy(device_seen_at=value), now=NOW)
    assert "seen" not in text


def test_a_naive_timestamp_is_read_as_utc():
    """Everything the agent publishes is UTC; a missing offset is not a reason to guess
    local time, which would silently shift the age by hours."""
    _member, text = classify_status(
        _healthy(device_seen_at="2026-08-02T11:00:00"), now=NOW
    )
    assert "seen 1h ago" in text


def test_a_clock_ahead_of_ours_reads_as_just_now_not_a_negative_age():
    """The agent's clock and InvenTree's are not the same clock."""
    _member, text = classify_status(_seen(timedelta(minutes=-5)), now=NOW)
    assert "seen just now" in text
    assert "-" not in text.split("seen")[-1]


# -- job outcomes ----------------------------------------------------------- #


def test_a_completed_job_reports_the_copies_actually_printed():
    member, text = classify_result(
        {
            "state": "completed",
            "labels": [{"index": 0, "copies_done": 2}, {"index": 1, "copies_done": 1}],
        },
        dispatched=2,
    )
    assert member == "CONNECTED"
    assert "printed 3 label(s)" in text


def test_no_result_inside_the_wait_stays_printing():
    """Strip mode holds labels until the coalescer flushes; that is not a failure."""
    member, text = classify_result(None, dispatched=2)
    assert member == "PRINTING"
    assert "awaiting confirmation" in text


def test_a_failed_job_is_an_error_carrying_the_reason():
    member, text = classify_result(
        {"state": "failed", "error": "printer cancelled the print"}, dispatched=1
    )
    assert member == "ERROR"
    assert "cancelled" in text


def test_partial_tape_is_flagged_because_a_blind_reprint_double_prints():
    _member, text = classify_result(
        {"state": "failed", "error": "disconnected mid-strip", "partial_tape_consumed": True},
        dispatched=5,
    )
    assert "tape partially consumed" in text


def test_a_stalled_job_reads_as_disconnected_not_failed():
    """Stalled means requeued and will retry, so it must not look like a lost job."""
    member, text = classify_result({"state": "stalled", "error": "printer asleep"}, dispatched=1)
    assert member == "DISCONNECTED"
    assert "requeued" in text


@pytest.mark.parametrize("state", ["completed", "partial", "failed", "stalled", "rejected"])
def test_every_contract_job_state_maps_somewhere_valid(state):
    member, text = classify_result({"state": state}, dispatched=1)
    assert member in VALID_MEMBERS
    assert text


def test_partial_is_terminal_and_says_what_actually_printed():
    """The agent sets partial from `any_printed and any_failed` when finalising a job.

    So it is terminal, not in-flight, and the operator needs the counts to know what to
    reprint -- falling through to a bare "job partial" tells them nothing.
    """
    member, text = classify_result(
        {
            "state": "partial",
            "labels": [
                {"index": 0, "state": "printed", "copies_done": 2},
                {"index": 1, "state": "failed", "copies_done": 0},
                {"index": 2, "state": "failed", "copies_done": 0},
            ],
        },
        dispatched=3,
    )
    assert member == "ERROR"
    assert "printed 2 of 3 label(s)" in text
    assert "2 failed" in text


def test_partial_still_flags_partial_tape():
    _member, text = classify_result(
        {
            "state": "partial",
            "partial_tape_consumed": True,
            "labels": [{"index": 0, "state": "printed", "copies_done": 1}],
        },
        dispatched=2,
    )
    assert "tape partially consumed" in text


def test_a_rejected_job_reports_why():
    """Rejection is a contract problem -- wrong model, bad spec -- so the reason matters."""
    member, text = classify_result(
        {"state": "rejected", "error": "this agent drives phomemo-d30, not phomemo-m110"},
        dispatched=1,
    )
    assert member == "ERROR"
    assert "phomemo-m110" in text
