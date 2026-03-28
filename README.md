# powervault-local

A standalone local controller for the **Powervault P3** home battery system,
written after Powervault entered administration in November 2024.

The controller talks directly to the hardware over RS485 and USB — no
dependency on the Powervault cloud, the M4 board's internal MQTT broker, or
any other external service. It publishes all sensor data to Home Assistant via
MQTT and accepts charge-mode commands from HA in return.

> ⚠️ **Disclaimer** — This is an unofficial community project. Use at your own
> risk. Modifying battery hardware may void warranties and could be dangerous
> if done incorrectly.

---

## Repository layout

```
controller/          Main control loop, HA discovery, BMS safety logic
p18_serial/          Voltronic P18 serial protocol driver (Iconica inverter)
pylontech_driver/    Pylontech RS485 BMS driver
abb_aurora/          ABB UNO PVI 3.0 Aurora RS485 driver (optional)
poll.py              CLI hardware poll utility (see below)
tests/               Unit tests
.env.example         All configuration options with explanations
docker-compose.yml   Docker deployment
```

---

## Hardware overview

| Component | Protocol | Port |
|-----------|----------|------|
| Iconica inverter (Voltronic InfiniSolar E 5.5 kW) | P18 RS-232 or USB HID | `/dev/ttyUSB0` or `/dev/hidraw0` |
| Pylontech US2000C battery stack | RS485 @ 115200 | `/dev/ttyUSB1` |
| ABB UNO PVI 3.0 solar inverter *(optional)* | Aurora RS485 @ 19200 | `/dev/ttyUSB2` |
| Shelly EM CT clamps *(optional)* | MQTT over WiFi | — |

> **USB isolator** — always use a USB isolator between the Raspberry Pi and the
> Iconica inverter to protect the Pi from ground loops.

---

## Quick start

### 1. Wire up the hardware

**Pylontech RS485**

Connect a USB-to-RS485 adapter to the RJ11/RJ12 RS485 port on the **master**
(lowest-address) battery module:

| Battery pin | RS485 wire |
|-------------|-----------|
| Pin 7 | A (positive) |
| Pin 8 | B (negative) |

Set DIP switch 1 on the master battery to **OFF** (selects 115200 baud).

**Iconica inverter**

Two options:
- **USB HID** — plug into the USB port on the inverter front panel
  (`/dev/hidraw0`, set `INVERTER_IS_USB=true`)
- **USB-serial** — connect a USB-to-RS232 adapter to the RS-232 port
  (`/dev/ttyUSB0`, set `INVERTER_IS_USB=false`)

**ABB UNO PVI 3.0 *(optional)***

Connect a USB-to-RS485 adapter to the RS485 terminals (A+, B−, GND) on the
inverter. Set `PV_INVERTER_PORT=/dev/ttyUSB2`.

If you do not have this inverter, leave `PV_INVERTER_PORT` blank — or use
`PV_GENERATION_TOPIC` to subscribe to an MQTT topic that another device
publishes PV power to instead (see [PV generation](#pv-generation-optional)).

**Shelly EM *(optional)***

Install the Shelly EM on your WiFi network and configure it to publish to your
MQTT broker. Set `SHELLY_GRID_POWER_TOPIC` (and optionally
`SHELLY_GRID_POWER_TOPIC_2` if you have two CT clamps on two fuse boards — the
controller will sum both channels automatically).

### 2. Configure

```bash
git clone https://github.com/adammcdonagh/powervault-local.git
cd powervault-local
cp .env.example .env
nano .env   # fill in your settings (see Configuration reference below)
```

Every setting in `.env.example` is documented with inline comments.  The
minimum you must change before first run:

| Variable | What to set |
|----------|-------------|
| `HA_MQTT_HOST` | IP address of your Home Assistant Mosquitto broker |
| `INVERTER_PORT` | Serial/USB device for the Iconica inverter (see [Discovering USB device paths](#discovering-usb-device-paths) below) |
| `BATTERY_PORT` | Serial/USB device for the Pylontech battery RS485 adapter |

### 3. Run

**Direct (Python 3.10+):**

```bash
pip install -r requirements.txt
python -m controller.main
```

**Docker (recommended on the Raspberry Pi):**

```bash
# Build the image and start the controller in the background.
# Reads .env automatically for all configuration.
docker compose up -d

# Follow logs
docker compose logs -f controller

# Stop
docker compose down
```

The controller publishes MQTT auto-discovery payloads on first run, so all
Home Assistant entities appear automatically — no manual YAML needed.

---

## Charge modes

Control the battery via the **Charge Mode** select entity that appears in HA:

| Mode | Behaviour |
|------|-----------|
| `idle` | Hold battery — neither charge from grid nor discharge to load |
| `charge` | Charge from grid/solar (stopped at 95 % SoC) |
| `discharge` | Discharge to power the house load (stopped at 15 % SoC) |
| `force_charge` | Maximum grid charge, bypasses the 95 % ceiling |
| `force_discharge` | Maximum discharge, bypasses the ceiling (15 % floor still applies) |

The BMS safety limits (current, voltage) read from the Pylontech stack are
programmed into the inverter on every poll cycle regardless of mode.

---

## PV generation *(optional)*

Two mutually exclusive options:

### Option A — Direct RS485 polling (ABB UNO PVI 3.0)

Set `PV_INVERTER_PORT` to the serial port connected to the inverter.  The
controller uses the Aurora protocol to read AC power, DC power, grid voltage /
frequency, temperature, and daily/total energy, publishing each as a separate
HA sensor.

### Option B — External MQTT topic

If you have any other solar inverter (or a Shelly device clamped to the PV
output), set `PV_GENERATION_TOPIC` to the topic it publishes AC power on.  The
payload can be a plain watt value (`"1500.0"`) or JSON containing one of the
common power keys (`apower`, `act_power`, `power`, etc.).  Only `pv_ac_power`
is populated via this path.

When `PV_GENERATION_TOPIC` is set it takes priority and `PV_INVERTER_PORT` is
ignored.

---

## Shelly EM grid power *(optional)*

Set `SHELLY_GRID_POWER_TOPIC` to the topic your Shelly EM publishes active
power on.  Supported payload formats:

| Device generation | Topic example | Payload |
|-------------------|---------------|---------|
| Gen 1 EM | `shellies/shellyem-aabbcc/emeter/0/power` | Plain float: `"1234.5"` |
| Gen 2 / Gen 3 | `shellyplusem-aabbcc/status/em:0` | JSON: `{"apower": 1234.5, ...}` |

**Two CT clamps / two fuse boards**

If you have one CT clamp on each of two fuse boards, set both topics:

```
SHELLY_GRID_POWER_TOPIC=shellies/shellyem-aabbcc/emeter/0/power
SHELLY_GRID_POWER_TOPIC_2=shellies/shellyem-aabbcc/emeter/1/power
```

The controller sums both channels and publishes the total as `grid_power`.

**Sign convention** — positive = importing from grid, negative = exporting.

---

## Peak-shaving *(optional)*

Requires `SHELLY_GRID_POWER_TOPIC`.

```
PEAK_SHAVE_ENABLED=true
PEAK_SHAVE_IMPORT_W=2000      # trigger discharge above this threshold
PEAK_SHAVE_HYSTERESIS_W=50    # deadband to prevent rapid toggling
PEAK_SHAVE_MIN_APPLY_INTERVAL=2  # minimum seconds between commands
```

When enabled, the controller automatically overrides the HA-selected mode with
`discharge` whenever grid import exceeds `PEAK_SHAVE_IMPORT_W +
PEAK_SHAVE_HYSTERESIS_W`.  The response time is within
`PEAK_SHAVE_MIN_APPLY_INTERVAL` seconds of a Shelly reading arriving (much
faster than the normal `POLL_INTERVAL` cycle).

---

## Configuration reference

All settings are read from environment variables (or a `.env` file).

| Variable | Default | Description |
|----------|---------|-------------|
| `HA_MQTT_HOST` | *(required)* | Home Assistant MQTT broker hostname or IP |
| `HA_MQTT_PORT` | `1883` | HA broker port |
| `HA_MQTT_USER` | | HA broker username (leave blank if not required) |
| `HA_MQTT_PASS` | | HA broker password |
| `DEVICE_ID` | `powervault` | Prefix used in all published MQTT topics |
| `POLL_INTERVAL` | `30` | Seconds between full BMS/PV polling cycles |
| `INVERTER_PORT` | `/dev/ttyUSB0` | Serial/USB port for the Iconica inverter |
| `INVERTER_IS_USB` | `false` | Set `true` to use the USB HID port (`/dev/hidraw0`) |
| `BATTERY_PORT` | `/dev/ttyUSB1` | RS485 port for the Pylontech battery stack |
| `NUM_BATTERY_MODULES` | `1` | Number of Pylontech modules (addresses 0 … N-1) |
| `PV_INVERTER_PORT` | | RS485 port for the ABB Aurora PV inverter; leave blank to disable |
| `PV_INVERTER_ADDRESS` | `2` | Aurora RS485 address |
| `PV_GENERATION_TOPIC` | | MQTT topic publishing PV AC power in watts; takes priority over `PV_INVERTER_PORT` when set |
| `SHELLY_GRID_POWER_TOPIC` | | Shelly EM channel 1 power topic; leave blank to disable |
| `SHELLY_GRID_POWER_TOPIC_2` | | Shelly EM channel 2 power topic (summed with channel 1) |
| `PEAK_SHAVE_ENABLED` | `false` | Enable automatic peak-shaving |
| `PEAK_SHAVE_IMPORT_W` | `0` | Grid import threshold in W |
| `PEAK_SHAVE_HYSTERESIS_W` | `50` | Deadband in W above threshold |
| `PEAK_SHAVE_MIN_APPLY_INTERVAL` | `2` | Minimum seconds between fast-path commands |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## MQTT topics published

The controller publishes all sensor readings to your HA MQTT broker under
`powervault/<DEVICE_ID>/sensor/<name>/state`.

**Battery (always published)**

| Sensor name | Unit | Description |
|-------------|------|-------------|
| `battery_soc` | % | State of Charge |
| `battery_soh` | % | State of Health (average across modules) |
| `battery_soh_min` | % | State of Health (lowest module) |
| `battery_cycle_count` | | Maximum cycle count across modules |
| `battery_voltage` | V | Pack voltage |
| `battery_current` | A | Pack current (positive = charging) |
| `battery_power` | W | Pack power |
| `battery_cell_voltage_max` | V | Highest cell voltage |
| `battery_cell_voltage_min` | V | Lowest cell voltage |
| `battery_cell_temp_avg` | °C | Average cell temperature |
| `battery_cell_temp_max` | °C | Hottest cell |
| `battery_cell_temp_min` | °C | Coolest cell |
| `bms_charge_current_limit` | A | BMS maximum charge current |
| `bms_discharge_current_limit` | A | BMS maximum discharge current |
| `bms_charge_voltage_limit` | V | BMS charge voltage limit |
| `bms_discharge_voltage_limit` | V | BMS discharge cutoff voltage |

**PV inverter (published when `PV_INVERTER_PORT` or `PV_GENERATION_TOPIC` is set)**

| Sensor name | Unit | Description |
|-------------|------|-------------|
| `pv_ac_power` | W | AC output power |
| `pv_dc_power` | W | DC input power from panels *(RS485 only)* |
| `pv_dc_voltage` | V | Panel-side voltage *(RS485 only)* |
| `pv_dc_current` | A | Panel-side current *(RS485 only)* |
| `pv_grid_voltage` | V | Grid voltage *(RS485 only)* |
| `pv_grid_frequency` | Hz | Grid frequency *(RS485 only)* |
| `pv_temperature` | °C | Inverter temperature *(RS485 only)* |
| `pv_energy_today` | kWh | Energy generated today *(RS485 only)* |
| `pv_energy_total` | kWh | Lifetime energy *(RS485 only)* |
| `pv_state` | | Inverter state string *(RS485 only)* |
| `pv_producing` | | Binary sensor — ON when producing *(RS485 only)* |

**Grid power (published when `SHELLY_GRID_POWER_TOPIC` is set)**

| Sensor name | Unit | Description |
|-------------|------|-------------|
| `grid_power` | W | Grid power — positive = import, negative = export |

**Control**

| Topic | Direction | Description |
|-------|-----------|-------------|
| `powervault/<ID>/control/charge_mode/set` | HA → controller | Set the charge mode |
| `powervault/<ID>/control/charge_mode/state` | Controller → HA | Current applied mode |
| `powervault/<ID>/availability` | Controller → HA | `online` / `offline` |

---

## P18 protocol notes

The Iconica inverter is a rebranded **Voltronic InfiniSolar E 5.5 kW** and
speaks the Voltronic P18 protocol over RS-232 (2400 baud, 8N1) or USB HID.

The protocol is documented by the community and implemented in various tools
including [inverter-tools](https://github.com/gch1p/inverter-tools).  The
`p18_serial/` module in this repo implements the subset of commands needed for
charge/discharge control:

| P18 command | Method | Description |
|-------------|--------|-------------|
| `^S006POP<code>` | `set_output_priority()` | Output source priority (UTI/SOL/SBU) |
| `^S006PCP<code>` | `set_charger_priority()` | Charger source priority (UTI/SOL/MIX/ONL) |
| `^S010MUCHGC<n>` | `set_max_charge_current()` | Total max charge current (A) |
| `^S010MCHGC<n>` | `set_ac_charge_current()` | Grid charge current (A) |
| `^S008PBCV<v>` | `set_battery_recharge_voltage()` | Re-charge voltage (V) |
| `^S009PSDV<v>` | `set_battery_cutoff_voltage()` | Low-voltage cutoff (V) |
| `^S010BATCD<flags>` | `set_battery_discharge_control()` | Grid charge / discharge flags |

> ⚠️ Commands are verified against Voltronic community documentation and
> the [inverter-tools](https://github.com/gch1p/inverter-tools) reference
> implementation.  Always verify against your firmware before relying on them.
> Call `normal_mode()` to return to safe defaults if anything behaves
> unexpectedly.

---

## Discovering USB device paths

Before running anything you need to know which `/dev` path each USB adapter
has been assigned.

### On the Raspberry Pi (Linux)

```bash
# List all USB serial adapters
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

# More detail — shows the USB vendor/product ID and which physical port it's in
ls -l /dev/serial/by-id/

# Confirm a device is responding (press Ctrl-C after a few seconds)
# Replace /dev/ttyUSB0 with your actual path
stty -F /dev/ttyUSB0 115200 && cat /dev/ttyUSB0
```

A typical mapping (in order of plug-in):

| Adapter | Typical path |
|---------|-------------|
| Iconica inverter (RS-232) | `/dev/ttyUSB0` |
| Pylontech RS485 | `/dev/ttyUSB1` |
| ABB Aurora RS485 | `/dev/ttyUSB2` |
| Iconica inverter (USB HID front panel) | `/dev/hidraw0` |

> **Tip:** The paths can shift if adapters are plugged/unplugged in a different
> order.  Use the stable symlinks under `/dev/serial/by-id/` in your `.env`
> file to avoid surprises after a reboot.

### On macOS

```bash
# List USB serial ports
ls /dev/tty.usb*

# Example output:
#   /dev/tty.usbserial-0001
#   /dev/tty.usbserial-0002
```

Adapters appear as `/dev/tty.usbserial-XXXX` (CP2102/CH340) or
`/dev/tty.usbmodem*` (FTDI).

---

## Hardware poll utility (`poll.py`)

`poll.py` is a standalone CLI tool for testing your hardware connections without
running the full controller.  It uses the same drivers as the controller and
prints a formatted summary of readings.

### Running directly (Python 3.10+)

```bash
pip install -r requirements.txt

# Pylontech batteries only
python poll.py --battery /dev/ttyUSB1

# Iconica inverter only (RS-232 / USB-serial adapter)
python poll.py --inverter /dev/ttyUSB0

# Iconica inverter via the USB HID port on the front panel
python poll.py --inverter /dev/hidraw0 --inverter-usb

# ABB Aurora PV inverter only
python poll.py --pv /dev/ttyUSB2

# All three at once
python poll.py \
    --battery  /dev/ttyUSB1 \
    --inverter /dev/ttyUSB0 \
    --pv       /dev/ttyUSB2

# Poll every 10 seconds (Ctrl-C to stop)
python poll.py --battery /dev/ttyUSB1 --interval 10

# Machine-readable JSON output
python poll.py --battery /dev/ttyUSB1 --json
```

On macOS, replace `/dev/ttyUSBx` with your adapter path (e.g.
`/dev/tty.usbserial-0001`).  See [Discovering USB device paths](#discovering-usb-device-paths) above.

### Running via Docker (Raspberry Pi)

The same Docker image used by the controller also contains `poll.py`.  This is
the easiest way to test hardware on the Pi without installing Python or any
dependencies on the host.

**Prerequisites:** your `.env` file is present and `BATTERY_PORT` /
`INVERTER_PORT` / `PV_INVERTER_PORT` are set to the correct `/dev` paths.

```bash
# Build the image (if not already built)
docker compose build

# Poll the battery only — passes /dev/ttyUSB1 into the container
docker compose run --rm poll --battery /dev/ttyUSB1

# Poll the inverter only
docker compose run --rm poll --inverter /dev/ttyUSB0

# Poll all three at once
docker compose run --rm poll \
    --battery  /dev/ttyUSB1 \
    --inverter /dev/ttyUSB0 \
    --pv       /dev/ttyUSB2

# Repeat every 10 seconds
docker compose run --rm poll --battery /dev/ttyUSB1 --interval 10

# JSON output (useful for piping into jq)
docker compose run --rm poll --battery /dev/ttyUSB1 --json | jq .

# Show all available flags
docker compose run --rm poll --help
```

> **How the device paths get into the container:**  
> `docker-compose.yml` passes the `INVERTER_PORT` and `BATTERY_PORT` values
> from your `.env` file as `devices:` entries.  If you want to pass a port that
> isn't in `.env` (e.g. the PV inverter), either add it to `.env` and
> uncomment the `PV_INVERTER_PORT` device line in `docker-compose.yml`, or use
> `docker run` directly (see below).

#### Using `docker run` directly (without docker-compose)

If you prefer not to use Compose, or need a port that isn't wired into the
`poll` service's `devices:` block, use `docker run` with `--device`:

```bash
# Build the image
docker build -t powervault-local .

# Single USB adapter (battery RS485)
docker run --rm -it \
    --device /dev/ttyUSB1:/dev/ttyUSB1 \
    powervault-local \
    python poll.py --battery /dev/ttyUSB1

# All three adapters
docker run --rm -it \
    --device /dev/ttyUSB0:/dev/ttyUSB0 \
    --device /dev/ttyUSB1:/dev/ttyUSB1 \
    --device /dev/ttyUSB2:/dev/ttyUSB2 \
    powervault-local \
    python poll.py \
        --battery  /dev/ttyUSB1 \
        --inverter /dev/ttyUSB0 \
        --pv       /dev/ttyUSB2

# Inverter front-panel USB HID port
docker run --rm -it \
    --device /dev/hidraw0:/dev/hidraw0 \
    powervault-local \
    python poll.py --inverter /dev/hidraw0 --inverter-usb
```

> **Note:** The path after the colon (`:/dev/ttyUSB1`) is the path *inside* the
> container — it must match the `--battery` / `--inverter` / `--pv` argument
> you pass to `poll.py`.  Keeping both sides identical (as above) avoids
> confusion.

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--battery PORT` | | RS485 port for the Pylontech battery stack |
| `--battery-modules N` | `1` | Number of Pylontech modules |
| `--inverter PORT` | | Serial/USB port for the Iconica inverter (P18) |
| `--inverter-usb` | | Use USB HID mode (inverter front-panel USB port) |
| `--pv PORT` | | RS485 port for the ABB Aurora PV inverter |
| `--pv-address N` | `2` | Aurora RS485 device address |
| `--interval SECS` | *(once)* | Repeat every N seconds |
| `--json` | | Emit JSON instead of the human-readable table |
| `--log-level LEVEL` | `WARNING` | Logging verbosity (`DEBUG`, `INFO`, …) |

---

## Running tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

---

## Contributing

Found something that works (or doesn't)? Please open an issue or PR.
Especially welcome: confirmed P18 command responses for other Voltronic
firmware versions, Pylontech module variants, and Shelly MQTT payload formats.

---

## Licence

MIT
