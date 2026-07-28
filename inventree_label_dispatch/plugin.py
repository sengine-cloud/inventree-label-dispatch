"""The plugin entry point InvenTree discovers via the ``inventree_plugins`` group.

A machine driver, not a ``LabelPrintingMixin``: many printers per plugin, a first-class
Machines admin page, and -- the deciding factor -- connection status in the UI, which
for an always-asleep Bluetooth printer is most of the usability.
"""

from __future__ import annotations

from plugin import InvenTreePlugin
from plugin.mixins import MachineDriverMixin

from .driver import LabelfabDriver
from .version import __version__


class LabelfabDispatchPlugin(MachineDriverMixin, InvenTreePlugin):
    NAME = "LabelfabDispatch"
    SLUG = "labelfab-dispatch"
    TITLE = "labelfab dispatch"
    DESCRIPTION = "Dispatch InvenTree labels to a labelfab print agent (Phomemo D30) over MQTT"
    AUTHOR = "sengine"
    VERSION = __version__
    WEBSITE = "https://github.com/sengine-cloud/inventree-label-dispatch"
    MIN_VERSION = "1.4.0"

    def get_machine_drivers(self):
        return [LabelfabDriver]
