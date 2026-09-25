"""Barcode plugin that resolves the short links printed on labelfab labels.

A plugin class of its own, beside the machine driver, so it can be enabled separately.
It only translates: ``https://sngn.top/i/SI1`` becomes ``INV-SI1`` and goes to
InvenTree's builtin barcode plugin, so model codes, permissions and the response shape
all stay InvenTree's. Without it, InvenTree answers "No match found" for every label
this package prints, and linking barcodes item by item does not scale.
"""

from __future__ import annotations

from plugin import InvenTreePlugin
from plugin.mixins import BarcodeMixin, SettingsMixin
from plugin.registry import registry

from . import shortlink
from .extractors import DEFAULT_PREFIX
from .version import __version__

#: InvenTree's builtin barcode plugin. It owns the short format and its prefix.
BUILTIN = "inventreebarcode"


def short_prefix() -> str:
    """The instance's short barcode prefix (``INV-`` unless someone changed it)."""
    builtin = registry.get_plugin(BUILTIN)
    if builtin is None:
        return DEFAULT_PREFIX
    return builtin.get_setting("SHORT_BARCODE_PREFIX") or DEFAULT_PREFIX


class LabelfabShortlinkPlugin(BarcodeMixin, SettingsMixin, InvenTreePlugin):
    NAME = "LabelfabShortlink"
    SLUG = "labelfab-shortlink"
    TITLE = "labelfab short links"
    DESCRIPTION = "Scan the short-link QR codes on labelfab labels, e.g. https://sngn.top/i/SI1"
    AUTHOR = "sengine"
    VERSION = __version__
    WEBSITE = "https://github.com/sengine-cloud/inventree-label-dispatch"
    MIN_VERSION = "1.4.0"

    SETTINGS = {
        "SHORTLINK_BASE": {
            "name": "Short link base",
            "description": (
                "The same value as the agent's render.qr_base_url, e.g. "
                "https://sngn.top/i/. Leave empty to disable."
            ),
            "default": "",
        },
    }

    def scan(self, barcode_data, user, **kwargs):
        parsed = shortlink.parse(barcode_data, self.get_setting("SHORTLINK_BASE"))
        if parsed is None:
            return None
        builtin = registry.get_plugin(BUILTIN)
        if builtin is None:
            return None
        code, pk = parsed
        return builtin.scan(f"{short_prefix()}{code}{pk}", user, **kwargs)
