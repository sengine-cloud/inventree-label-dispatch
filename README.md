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

## What the status actually says

The retained status carries the printer's own report — firmware, battery percentage
*and* terminal voltage, and whether media is loaded — so the Machines page reflects the
device rather than just the agent's liveness. The mapping lives in `status.py`, free of
InvenTree imports so it is unit-tested here:

| Printer reports | Machines status |
|---|---|
| nothing retained | `UNKNOWN` — the agent has never published |
| `state: idle`, media ok | `CONNECTED` |
| media bit clear (`0x06`) | **`NO_MEDIA`** — "load tape", not "investigate" |
| any other fault | `ERROR`, carrying the reason |
| agent will / shutdown | `DISCONNECTED` |

Two deliberate distinctions. **Unreported media is not healthy media**: `media_ok` is a
tri-state, and a printer that has said nothing shows "media unreported" rather than
"media ok". And **battery percentage alone is not enough** — it pins at 100% whenever
the unit is on charge, so the voltage is shown beside it.

## Print outcomes

`AWAIT_RESULT_S` makes the driver wait for the job's `JobResult` after dispatching, so
the UI reports what actually happened instead of "dispatched". It subscribes *before*
publishing — the results topic is not retained, so subscribing afterwards races the
agent — and filters on `job_id`, because one printer serves every user and a stranger's
result must not be mistaken for yours.

**It is off by default (`0`).** The wait holds a Django-Q worker, and in strip mode the
agent deliberately buffers labels until its coalescer flushes — 30s by default — so the
wait would usually time out and leave the status to the retained topic regardless.
Enable it on die-cut/discrete media, where a result comes back in a few seconds.

A timeout is an ordinary outcome, not a failure: the status stays `PRINTING` and the
next refresh picks up the truth from the retained topic.

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
