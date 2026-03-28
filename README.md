# powervault-local

Local monitoring and control for the **Powervault P3** home battery system,
following the company entering administration in November 2024.

> ⚠️ **Disclaimer** — This is an unofficial community project. Use at your own
> risk. Modifying your battery system may void warranties and could be unsafe
> if done incorrectly.

---

## What's in the box

| Component | Description |
|-----------|-------------|
| `ha-config/` | Ready-to-use Home Assistant YAML sensor, alarm and dashboard files |
| `pv3_monitor/` | Python MQTT bridge with Home Assistant **auto-discovery** |
| `p18_serial/` | P18 serial protocol driver for direct inverter control |

---

## System Overview

The Powervault P3 contains three key sub-systems:

| Component | Detail |
|-----------|--------|
| **M4 Controller** | Custom NXP i.MX ARM board running embedded Linux |
| **Iconica Inverter** | Rebranded Voltronic InfiniSolar E 5.5 kW (P18 protocol) |
| **Pylontech Batteries** | US2000C modules, CAN/RS-485 BMS |
| **FFR Metering** | CT clamps for grid / house / aux power measurement |

### Current Status

| Capability | Status | Notes |
|------------|--------|-------|
| **MQTT Monitoring** | ✅ Working | All metrics available, no auth required |
| **Home Assistant Integration** | ✅ Working | Static YAML or auto-discovery bridge |
| **Battery health data** | ✅ Working | SOH, cycles, cell voltages, temperatures |
| **Inverter alarms** | ✅ Working | All 20+ alarm states |
| **Schedule monitoring** | ✅ Working | Current event (0–4) and setpoint |
| **MQTT Control** | ❌ Not working | M4 ignores locally published messages |
| **SSH Schedule Control** | 🔄 Pending | Needs SSH access to M4 confirmed |
| **P18 Direct Serial** | 🔄 Pending | Needs physical serial port access |

---

## Quick Start — Monitoring (Works Today)

### 1. Find the P3's IP address

Check your router's DHCP client list for a device with MAC prefix `00:1F:7B`.

### 2. Verify MQTT data is flowing

```bash
mosquitto_sub -h 192.168.1.215 -t 'pv/#' -v
```

You should immediately see JSON messages on topics like:
```
pv/PV3/PV001001DEV/bms/soc
pv/PV3/PV001001DEV/inverter/measurements
pv/PV3/PV001001DEV/ffr/measurements
...
```

### 3. Note your Device ID

The device ID is the third path segment, e.g. `PV001001DEV`. Replace it
throughout the YAML files.

---

## Option A — Static HA YAML (Simplest)

Connect Home Assistant **directly** to the P3's built-in MQTT broker. No
extra software required.

1. Add the P3 MQTT broker in `configuration.yaml`:

   ```yaml
   mqtt:
     broker: 192.168.1.215   # ← your P3 IP
     port: 1883
   ```

2. Copy the YAML files:

   ```bash
   cp ha-config/mqtt_sensors.yaml     /config/
   cp ha-config/pylontech_sensors.yaml /config/
   cp ha-config/alarm_sensors.yaml     /config/
   ```

3. Replace the placeholder device ID:

   ```bash
   sed -i 's/PV001001DEV/YOUR_ACTUAL_ID/g' /config/mqtt_sensors.yaml \
     /config/pylontech_sensors.yaml /config/alarm_sensors.yaml
   ```

4. Add to `configuration.yaml`:

   ```yaml
   mqtt: !include mqtt_sensors.yaml
   ```

   Or append the `sensor:` sections to an existing `mqtt.yaml`.

5. Copy the dashboard — see [`ha-config/README.md`](ha-config/README.md).

6. Restart Home Assistant.

---

## Option B — Python MQTT Bridge (Auto-Discovery)

The bridge subscribes to the P3's broker, processes every message, and
publishes Home Assistant MQTT auto-discovery payloads so entities appear
automatically without any manual YAML.

### Prerequisites

- Python 3.10+
- Access to your HA MQTT broker (username/password if required)

### Setup

```bash
# Clone the repo
git clone https://github.com/adammcdonagh/powervault-local.git
cd powervault-local

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your P3 IP, device ID, and HA MQTT details
nano .env

# Run
python -m pv3_monitor.bridge
```

### Docker

```bash
cp .env.example .env
nano .env          # fill in your settings
docker-compose up -d
```

### What the bridge publishes

For each sensor reading extracted from a P3 MQTT message the bridge:

1. Publishes a **discovery config** to `homeassistant/sensor/<device_id>/pv3_<name>/config`
   (retained, published once per sensor).
2. Publishes the **current value** to `powervault/<device_id>/sensor/<name>/state`.

For control, it also publishes:
- A `select` entity for schedule mode (Idle / Charge / Discharge / Force Charge / Force Discharge)
- A `number` entity for the power setpoint (0–5500 W)

---

## Control

> **Note**: Publishing to the P3's MQTT broker does **not** control the system.
> The M4 ignores all locally published messages — commands must arrive via the
> Powervault cloud authentication path, which is no longer available.

Two local control paths are possible:

### Path 1 — SSH Schedule Control (Recommended)

The M4 reads charge/discharge schedules from a JSON file:
```
/data/appconfigs/cloudconnection/state_schedules/ffr_schedule.json
```

If you can establish SSH access to the M4 (serial console → enable SSH, or
other access methods), the `ScheduleController` class can write this file:

```python
from pv3_monitor.schedule_control import ScheduleController, FORCE_CHARGE

ctrl = ScheduleController(host="192.168.1.215", username="root")

# Force charge from grid
ctrl.force_charge(watts=3000)

# Return to idle
ctrl.idle()
```

**Schedule event codes** (confirmed via MQTT monitoring):

| Code | Mode |
|------|------|
| 0 | Idle |
| 1 | Charge |
| 2 | Discharge |
| 3 | Force Charge |
| 4 | Force Discharge |

Once SSH access is configured, the Python bridge exposes these modes as a
Home Assistant `select` entity automatically.

#### Getting SSH Access to the M4

The most reliable path to SSH access is via the **DB9 RS-232 serial console**
on the M4 board:

1. Connect a USB-to-RS232 adapter to the DB9 port on the M4.
2. Connect at **115200 baud, 8N1, no flow control**.
3. Power-cycle the P3 and watch for U-Boot / Linux boot output.
4. Log in at the `root@powervault:~#` prompt (or `powervault login:` prompt).
5. Enable SSH: `systemctl enable --now sshd` (or equivalent).
6. Add your public key to `/root/.ssh/authorized_keys`.

> ⚠️ Accessing the M4's operating system may void your warranty and should
> only be attempted once you understand the risks.

### Path 2 — P18 Direct Serial (Advanced)

The M4 communicates with the Iconica inverter over an internal RS-232 link
using the **Voltronic P18 protocol**. If you can tap this line or connect
directly to the inverter's own RS-232/USB port, the `P18Inverter` class
provides full control:

```python
from p18_serial.p18 import P18Inverter

with P18Inverter(port="/dev/ttyUSB0", baud_rate=2400) as inv:
    # Query protocol
    print(inv.get_protocol_id())   # → "PI18"
    print(inv.get_mode())          # → "L" (Line mode)

    # Force charge from grid
    inv.force_charge(ac_amps=30)

    # Return to normal solar/battery priority
    inv.normal_mode()
```

**Key P18 commands implemented:**

| Method | P18 Command | Description |
|--------|-------------|-------------|
| `set_output_priority("UTI")` | `^S006POP00` | Grid first |
| `set_output_priority("SBU")` | `^S006POP02` | Solar → Battery → Grid |
| `set_charger_priority("UTI")` | `^S006PCP00` | Charge from grid |
| `set_charger_priority("SOL")` | `^S006PCP01` | Charge from solar |
| `set_max_charge_current(30)` | `^S010MUCHGC030` | Max charge current |
| `set_ac_charge_current(30)` | `^S010MCHGC030` | Grid charge current |
| `set_battery_discharge_control(...)` | `^S010BATCD...` | Enable/disable charge/discharge |
| `force_charge()` | composite | Grid charge, no discharge |
| `normal_mode()` | composite | SBU priority, solar charge |

> ⚠️ The P18 command set documented here is based on Voltronic community
> documentation. Verify each command against your firmware before use.
> **Always back out to `normal_mode()` if the system behaves unexpectedly.**

#### Hardware connection

The inverter can be accessed via:
- **USB HID** port on the inverter front panel (`/dev/hidraw0`) — use `is_usb=True`
- **RS-232** serial port (`/dev/ttyUSB0`) — 2400 baud, 8N1

A serial proxy/intercept of the existing M4 ↔ inverter line is also possible
but requires physical access to the internal cabling.

---

## MQTT Topics Reference

All topics are published by the P3's M4 controller. All are read-only unless
you have shell access to the M4.

| Topic | Payload | Notes |
|-------|---------|-------|
| `pv/PV3/<ID>/bms/soc` | `[{"measurement":"StateOfCharge","value":9700}]` | 9700 = 97.00% |
| `pv/PV3/<ID>/inverter/measurements` | `[{"channel":"BATTERY","measurement":"Voltage","value":49800}]` | mV/mA/mW |
| `pv/PV3/<ID>/inverter/alarms` | `{"fan_lock":"0", ..., "inverter_temperature":42.5}` | Bit flags + temps |
| `pv/PV3/<ID>/inverter/charge` | `{"power":591}` | W; positive=discharge |
| `pv/PV3/<ID>/pylontech/info` | `[{"measurement":"StateOfHealth","type":"Avg","value":92}]` | milli-units |
| `pv/PV3/<ID>/ffr/measurements` | `[{"channel":"LOCAL","measurement":"Power","type":"Active","value":11736}]` | mW |
| `pv/PV3/<ID>/schedule/event` | `{"event":0,"setpoint":0}` | event 0–4 |
| `pv/PV3/<ID>/m4/maxpower` | `[{"ChgPower":4792,"DchgPower":-6750}]` | W |
| `pv/PV3/<ID>/eps/status` | `{"Reserve":20,"Mode":0}` | % reserve |
| `pv/PV3/<ID>/eps_schedule/event` | `{"reserved_soc":0,"event":"off"}` | EPS reserve |
| `pv/PV3/<ID>/ffrcontroller/state` | `{"State":0}` | FFR controller |
| `pv/PV3/<ID>/safetycheck/state` | various | Safety limits |

### FFR CT channel mapping

> **The physical CT clamp labels on the M4 board are swapped in software:**

| MQTT channel | Physical label | Measures |
|--------------|----------------|----------|
| `LOCAL` | LOCAL | House consumption |
| `HOUSE` | HOUSE | Grid power (+ import / − export) |
| `AUX1` | AUX1 | Auxiliary circuit |

---

## Running Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

All 93 tests should pass.

---

## Hardware Notes

- The M4 controller MAC address prefix is `00:1F:7B`.
- The M4 boots from **eMMC** (not SD card).
- The DB9 serial port on the M4 is the recommended path for console access.
- The rotary hex switch (0–F) on the M4 edge may control RS-485 device address
  or boot mode — note its current position before changing.

---

## Contributing

Found something that works (or doesn't)? Please open an issue or PR with:
- Your unit model / serial prefix
- Firmware version if known
- What you tested and what happened

Especially valuable: confirmed P18 command responses, SSH/serial console
access methods, and schedule file formats.

---

## Related Resources

- [Community findings repository (kevin-bird/powervault-p3-local)](https://github.com/kevin-bird/powervault-p3-local)
- [Powervault Owners Facebook Group](https://www.facebook.com/groups/powervaultowners)

---

## Licence

MIT — see [LICENSE](LICENSE) file.