"""Map the short links printed in label QR codes back to InvenTree's short barcodes.

The agent prints ``<qr_base_url><code>``, for example ``https://sngn.top/i/SI1``, so a
phone camera gets a URL it can open. InvenTree's own scanner only resolves its native
short format, ``INV-SI1``. Parsing lives here with no InvenTree imports so it can be
tested on its own; the plugin in ``barcode`` does the lookup.
"""

from __future__ import annotations

import re

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
#: Two-character model code, then the primary key. InvenTree's codes are uppercase
#: letters (PA, SI, SL, ...); an optional trailing slash tolerates hand-typed links.
_CODE = re.compile(r"([0-9A-Z]{2})(\d+)/?")


def _bare(url: str) -> str:
    """Drop the scheme and lowercase, so ``HTTPS://SNGN.TOP/I/`` equals ``sngn.top/i/``."""
    return _SCHEME.sub("", url.strip()).lower()


def parse(data, base: str) -> tuple[str, int] | None:
    """Return ``(model_code, pk)`` when ``data`` is a short link under ``base``.

    Scheme and case are ignored on both sides: the same label may carry the bare
    uppercase form (``SNGN.TOP/I/PA39``, which keeps the QR in alphanumeric mode) or a
    full URL (``https://sngn.top/i/PA39``, which phones open directly). An empty
    ``base`` matches nothing, so the plugin stays inert until it is configured.
    """
    if not base or not isinstance(data, str):
        return None
    prefix = _bare(base)
    bare = _bare(data)
    if not bare.startswith(prefix):
        return None
    match = _CODE.fullmatch(bare.removeprefix(prefix).lstrip("/").upper())
    if match is None:
        return None
    return match.group(1), int(match.group(2))
