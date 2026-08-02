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

### When it refreshes

The status is read from the retained topic in three places, which between them keep the
page honest without polling the printer itself:

| when | hook | asks the printer? |
|---|---|---|
| machine initialisation | `init_machine` | no — reads the retained topic |
| **Restart Machine** in the Admin Center | `restart_machine` | **yes** — publishes `probe` on the `cmd` topic |
| periodically | `ping_machines`, driven by InvenTree's `MACHINE_PING_ENABLED` (on by default) | no |

**"Restart Machine" is the refresh button.** InvenTree's machine framework has no generic
custom-action mechanism — `restart_machine` is the one hardcoded action, with its own
endpoint and a fixed place in the menu — so that item is the only lever a driver gets.
It now sends `probe` and the agent goes and looks at the printer, rather than re-reading
the retained topic the page was already showing. The `cmd` topic already existed for
`flush`, and the broker ACL already grants `inventree` write on it, so this needed no
new topic and no credential change.

The automatic paths deliberately do *not* probe: neither startup nor the periodic ping
should dial a sleeping printer on a schedule nobody asked for. And a probe the printer
cannot answer falls back to remembered truth, so the page keeps the old reading and its
age rather than going blank.

The periodic one is not a nicety. Machine status lives in the Django cache, so clearing
that cache drops every `machine:*` key at once — and this deployment runs Redis without
persistence, so a restart does exactly that. With only `init_machine` the page then sat
at `UNKNOWN` with an empty status text until someone printed or the pods restarted.

Three deliberate distinctions. **Unreported media is not healthy media**: `media_ok` is a
tri-state, and a printer that has said nothing shows "media unreported" rather than
"media ok". **Battery percentage alone is not enough** — it pins at 100% whenever
the unit is on charge, so the voltage is shown beside it. And **a memory is not an
observation**: the D30 is only reachable while it is being printed to, so the agent
republishes what it last heard rather than going quiet, and the age of that reading is
shown alongside it.

```
CONNECTED   fw 2.1.2 · 100% (4.17V) · media ok · Q223P4C31420105 · seen just now
CONNECTED   fw 2.1.2 · 100% (4.17V) · media ok · Q223P4C31420105 · seen 6h ago
```

The age comes from `device_seen_at` on the retained status. It annotates the reading
rather than overruling it — a printer that had tape six hours ago is far likelier to
still have tape than to have anything else — so the status mapping is unchanged. An
agent too old to publish the field renders exactly as it did before.

One case the age cannot cover: the agent's MQTT will is fixed when its connection opens
and the broker holds it, so a printer fault learned *during* that session cannot reach
it. If the agent is then killed, the broker publishes the older, healthier reading over
the newer one, and this page renders it — correctly marked `DISCONNECTED`, but with a
stale-healthy detail line. It is self-correcting rather than sticky: the agent restarts
(`Restart=always`, `RestartSec=5`), seeds from its spool, which does have the fault, and
republishes. Nothing here can detect the window, since retained MQTT v3.1.1 messages
carry no publish timestamp.

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
