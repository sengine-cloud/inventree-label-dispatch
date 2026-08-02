"""InvenTree label-printer driver that dispatches to a labelfab agent.

Thin by design: everything worth testing lives in ``dispatch``/``extractors``. This
class only reads settings, turns items into label specs, and publishes.

Two InvenTree-specific choices, both from the plan:

* The connection **status** is read from the printer's retained MQTT topic, so the
  Machines UI shows a sleeping D30 as disconnected *before* anyone clicks print --
  which is most of the UX for a Bluetooth printer that is asleep by default.
* ``server_render`` is an escape hatch: instead of sending ``{preset, vars}`` for the
  agent to lay out, render the InvenTree label template to PNG here and send that as a
  ``raw_png`` element. It de-risks the whole "agent owns layout" bet -- one awkward
  label becomes a checkbox, not a code change.
"""

from __future__ import annotations

import base64
import io

from django.utils.translation import gettext_lazy as _
from machine.machine_types import LabelPrinterBaseDriver

# LabelPrinterStatus is defined in the submodule and NOT re-exported from the
# machine.machine_types package (only LabelPrinterBaseDriver / LabelPrinterMachine
# are). Importing it from the package raises ImportError, which makes InvenTree drop
# the whole plugin silently -- so it must come from the submodule.
from machine.machine_types.label_printer import LabelPrinterStatus
from rest_framework import serializers

from .config import connection_from_env
from .dispatch import (
    build_job,
    build_label,
    probe_status,
    publish,
    publish_awaiting_result,
    read_status,
)
from .extractors import extract
from .status import classify_result, classify_status


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class LabelfabDriver(LabelPrinterBaseDriver):
    """Publishes print jobs to a labelfab agent over MQTT."""

    SLUG = "labelfab"
    NAME = "labelfab"
    DESCRIPTION = _("Dispatch labels to a labelfab print agent (Phomemo D30) over MQTT")

    MACHINE_SETTINGS = {
        "PRINTER_ID": {
            "name": _("Printer ID"),
            "description": _("labelfab printer id; the MQTT topic subtree it owns"),
            "default": "d30-workshop",
        },
        "TAPE_WIDTH_MM": {
            "name": _("Tape width (mm)"),
            "description": _("Continuous tape width loaded in the printer"),
            "default": "15",
        },
        "TAPE_KIND": {
            "name": _("Tape kind"),
            "description": _("continuous, or gap for die-cut media (forces discrete mode)"),
            "default": "continuous",
            "choices": [("continuous", "continuous"), ("gap", "gap")],
        },
        "DEFAULT_PRESET": {
            "name": _("Default preset"),
            "description": _("labelfab layout preset used when a template names none"),
            "default": "stock_item",
        },
        "SERVER_RENDER": {
            "name": _("Render on the server"),
            "description": _("Render the InvenTree template to PNG here instead of sending a preset"),
            "default": False,
            "validator": bool,
        },
        "AWAIT_RESULT_S": {
            "name": _("Wait for the print result (seconds)"),
            "description": _(
                "Block after dispatching to report the real outcome instead of just "
                "'dispatched'. Off by default: it holds a Django-Q worker for up to this "
                "long, and in strip mode the agent deliberately buffers labels until its "
                "coalescer flushes (30s by default), so the wait usually times out and "
                "the status is left to the retained topic anyway. Worth enabling on "
                "die-cut/discrete media, where a result comes back in a few seconds."
            ),
            "default": "0",
            "validator": float,
        },
    }

    class PrintingOptionsSerializer(LabelPrinterBaseDriver.PrintingOptionsSerializer):
        tape_width_mm = serializers.FloatField(
            required=False, label=_("Tape width (mm)"), help_text=_("Override the machine default")
        )
        label_length_mm = serializers.FloatField(
            required=False, label=_("Label length (mm)"), help_text=_("Blank = auto-size to content")
        )
        server_render = serializers.BooleanField(
            required=False, default=False, label=_("Render on server")
        )

    # -- lifecycle ---------------------------------------------------------- #

    def init_machine(self, machine) -> None:
        self._refresh_status(machine)

    def _apply(self, machine, member: str, text: str) -> None:
        """Set a status by member name, so the mapping stays free of InvenTree imports."""
        machine.set_status(getattr(LabelPrinterStatus, member))
        machine.set_status_text(text)

    def restart_machine(self, machine) -> None:
        """Ask the printer what it is, and show the answer. The page's refresh button.

        A restart clears the machine's shared state, and without this nothing puts it
        back: the page then shows UNKNOWN with an empty status text -- the defaults
        `get_shared_state` returns for missing keys -- while the machine itself
        reports `initialized`, no errors and a driver available. An empty status text
        is diagnostic on its own, since no path in this driver produces one.

        This is also the only lever there is. InvenTree's machine framework has no
        generic custom-action mechanism -- `restart_machine` is the one hardcoded
        action, with its own endpoint and a fixed place in the Machines menu -- so
        "Restart Machine" *is* the refresh button whether or not it is called that.
        Making it re-read the retained topic and nothing else wasted it: the topic
        holds what the agent last heard, which is the same thing the page was already
        showing. Now it sends `probe` and the agent goes and looks.

        Falls back to remembered truth when the printer does not answer, which is the
        normal case for a D30 that has powered itself down: the reading keeps its old
        `device_seen_at` and the page says how old it is, instead of going blank.
        """
        self._refresh_status(machine, probe=True)

    def ping_machines(self) -> None:
        """Periodic refresh, driven by InvenTree's MACHINE_PING_ENABLED (default on).

        This is what keeps the page honest between prints. It matters more than it
        looks: machine status lives in the Django cache, so when that cache is cleared
        -- a Redis restart, and this deployment runs Redis deliberately without
        persistence -- every `machine:*` key vanishes at once. Before this hook the
        only thing that re-read the retained topic was machine initialisation, so the
        page stayed blank until someone printed or the pods restarted.

        One machine's broker trouble must not stop the others being refreshed, hence
        the per-machine guard. `_refresh_status` already turns an unreachable broker
        into an UNKNOWN status rather than an exception; this catches anything else.
        """
        for machine in self.get_machines():
            try:
                self._refresh_status(machine)
            except Exception as exc:  # noqa: BLE001 - one machine must not break the loop
                machine.handle_error(exc)

    def _refresh_status(self, machine, *, probe: bool = False) -> None:
        """Read the retained status, or ask the printer for a fresh one.

        ``probe`` is off for the automatic paths -- init and the periodic ping -- so
        neither of them dials a sleeping printer on a schedule nobody asked for. It is
        on only where a human pressed the button.
        """
        printer_id = machine.get_setting("PRINTER_ID", "D")
        try:
            conn = connection_from_env()
            status = probe_status(printer_id, conn) if probe else read_status(printer_id, conn)
        except Exception as exc:
            machine.set_status(LabelPrinterStatus.UNKNOWN)
            machine.set_status_text(_("could not reach the broker: %(err)s") % {"err": str(exc)[:80]})
            return
        self._apply(machine, *classify_status(status))

    # -- printing ----------------------------------------------------------- #

    def print_labels(self, machine, label, items, **kwargs):
        options = kwargs.get("printing_options", {}) or {}
        copies = int(options.get("copies", 1) or 1)
        server_render = options.get("server_render") or _as_bool(
            machine.get_setting("SERVER_RENDER", "D")
        )
        preset = self._preset_for(machine, label)
        tape_width = float(
            options.get("tape_width_mm") or machine.get_setting("TAPE_WIDTH_MM", "D") or 15
        )
        tape_kind = machine.get_setting("TAPE_KIND", "D") or "continuous"
        length = options.get("label_length_mm")

        labels = []
        for item in items:
            if server_render:
                png = self._png(label, item)
                spec = {"elements": [{"type": "raw_png", "data_b64": png, "fit": "contain"}]}
            else:
                spec = {"preset": preset, "vars": extract(item)}
            labels.append(build_label(spec, copies=copies, length_mm=length))

        job = build_job(
            machine.get_setting("PRINTER_ID", "D"),
            labels,
            tape_width_mm=tape_width,
            tape_kind=tape_kind,
            batch_mode="discrete" if tape_kind == "gap" else "strip",
            template_id=getattr(label, "pk", None),
        )
        conn = connection_from_env()
        await_s = float(machine.get_setting("AWAIT_RESULT_S", "D") or 0)
        try:
            # Publishing is the only step that can fail locally. A print that fails at
            # the printer comes back as a JobResult, not an exception.
            if await_s > 0:
                result = publish_awaiting_result(job, conn, await_s)
            else:
                publish(job, conn)
                result = None
        except Exception as exc:
            machine.set_status(LabelPrinterStatus.DISCONNECTED)
            machine.set_status_text(_("dispatch failed: %(err)s") % {"err": str(exc)[:120]})
            raise

        if await_s > 0:
            self._apply(machine, *classify_result(result, dispatched=len(labels)))
        else:
            self._apply(machine, "PRINTING", _("dispatched %(n)d label(s)") % {"n": len(labels)})

    # -- helpers ------------------------------------------------------------ #

    def _preset_for(self, machine, label) -> str:
        """Template metadata wins, so a template can select a preset without code."""
        meta = getattr(label, "metadata", None) or {}
        chosen = (meta.get("labelfab") or {}).get("preset") if isinstance(meta, dict) else None
        return chosen or machine.get_setting("DEFAULT_PRESET", "D") or "stock_item"

    def _png(self, label, item) -> str:
        image = self.render_to_png(label, item, dpi=203)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
