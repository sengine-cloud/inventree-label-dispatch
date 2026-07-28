# inventree-label-dispatch

An InvenTree **machine driver** that dispatches label print jobs to a
[labelfab](https://github.com/sengine-cloud/labelfab) agent over MQTT. Click *Print*
in InvenTree; a job lands on the broker; the agent renders and prints it on a Phomemo
D30.

## Why a machine driver, not a `LabelPrintingMixin`

`MachineDriverMixin` + `LabelPrinterBaseDriver` gives many printers per plugin, a
first-class Machines admin page, and — the deciding factor — **connection status in
the UI**. A Bluetooth D30 is asleep by default; surfacing that before someone clicks
print is most of the usability. Status is read from the printer's retained MQTT topic,
so there is no polling loop in the Django worker.

## How it works

- Default path sends `{preset, vars}` — the producer stays dumb and layout ownership
  stays in the agent. Extractors map a `StockItem` / `Part` / `StockLocation` to a
  short scannable code plus title/sub text.
- `server_render` (a per-machine setting or a per-print option) is the escape hatch:
  render the InvenTree label template to PNG here and send it as a `raw_png` element.
  One awkward label becomes a checkbox, not a code change.
- Publishing uses a **short-lived connection per job** (connect → QoS 1 → disconnect).
  Django-Q workers are forked, and a persistent client across a fork silently drops
  publishes.

## Configuration

The MQTT connection comes from the environment, on **both** the InvenTree server and
the Django-Q worker (that is where `print_labels` runs):

| Variable | Default | Notes |
|---|---|---|
| `LABELFAB_MQTT_HOST` | — | in-cluster broker service |
| `LABELFAB_MQTT_PORT` | `1883` | |
| `LABELFAB_MQTT_USERNAME` | `inventree` | |
| `LABELFAB_MQTT_PASSWORD` | — | from Vault via VSO; never a machine setting |
| `LABELFAB_MQTT_TRANSPORT` | `tcp` | `tcp` in-cluster, `websockets` off-cluster |
| `LABELFAB_MQTT_TLS` | `false` | |
| `LABELFAB_MQTT_TOPIC_PREFIX` | `se/v1/print` | |

Per-printer settings (Printer ID, tape width/kind, default preset, server render)
live on the machine in the InvenTree admin.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest        # covers the pure dispatch + extractor logic
ruff check inventree_label_dispatch tests
```

The driver and plugin modules import InvenTree's `machine` / `plugin` packages, so
they are validated on a running InvenTree instance rather than in unit tests.

## License

MIT.
