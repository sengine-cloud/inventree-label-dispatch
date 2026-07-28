"""MQTT connection from the environment.

The broker host/user come from the deployment environment, and the password from a
Vault-backed secret projected into it -- on **both** the InvenTree server and the
Django-Q worker, since that is where ``print_labels`` actually runs. Nothing lands in
a machine setting, because those persist to the database and into backups.
"""

from __future__ import annotations

import os

from .dispatch import Connection


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def connection_from_env() -> Connection:
    return Connection(
        host=os.environ.get("LABELFAB_MQTT_HOST", ""),
        port=int(os.environ.get("LABELFAB_MQTT_PORT", "1883")),
        username=os.environ.get("LABELFAB_MQTT_USERNAME", "inventree"),
        password=os.environ.get("LABELFAB_MQTT_PASSWORD", ""),
        tls=_bool(os.environ.get("LABELFAB_MQTT_TLS", "false")),
        transport=os.environ.get("LABELFAB_MQTT_TRANSPORT", "tcp"),
        ws_path=os.environ.get("LABELFAB_MQTT_WS_PATH", "/mqtt"),
        topic_prefix=os.environ.get("LABELFAB_MQTT_TOPIC_PREFIX", "se/v1/print"),
    )
