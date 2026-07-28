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
from machine.machine_types import LabelPrinterBaseDriver, LabelPrinterStatus
from rest_framework import serializers

from .config import connection_from_env
from .dispatch import build_job, build_label, publish, read_status
from .extractors import extract


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

    def _refresh_status(self, machine) -> None:
        printer_id = machine.get_setting("PRINTER_ID", "D")
        try:
            status = read_status(printer_id, connection_from_env())
        except Exception:
            machine.set_status(LabelPrinterStatus.UNKNOWN)
            machine.set_status_text(_("could not reach the broker"))
            return
        if status is None:
            machine.set_status(LabelPrinterStatus.UNKNOWN)
        elif status.get("state") == "disconnected":
            machine.set_status(LabelPrinterStatus.DISCONNECTED)
        else:
            machine.set_status(LabelPrinterStatus.CONNECTED)

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
        try:
            publish(job, connection_from_env())
        except Exception as exc:
            machine.set_status(LabelPrinterStatus.DISCONNECTED)
            machine.set_status_text(_("dispatch failed: %(err)s") % {"err": str(exc)[:120]})
            raise
        machine.set_status(LabelPrinterStatus.PRINTING)
        machine.set_status_text(_("dispatched %(n)d label(s)") % {"n": len(labels)})

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
