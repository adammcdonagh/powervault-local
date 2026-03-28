# Home Assistant Configuration Files

This directory contains static YAML files for integrating the Powervault P3
with Home Assistant via MQTT.

## Quick Start (Static YAML — works today)

These files work by pointing Home Assistant **directly** at the P3's built-in
MQTT broker. No extra software is needed.

### 1. Find your P3's IP address

Check your router's DHCP client list for a device with MAC prefix `00:1F:7B`.

### 2. Verify MQTT is accessible

```bash
mosquitto_sub -h <P3_IP> -t 'pv/#' -v
```

You should immediately see JSON data flowing. If not, check that your machine
can reach port 1883 on the P3's IP.

### 3. Find your Device ID

The device ID appears in every topic, e.g.:

```
pv/PV3/PV001001DEV/bms/soc
          ^^^^^^^^^^^
          device ID
```

### 4. Configure Home Assistant

Add the P3 MQTT broker to your `configuration.yaml`:

```yaml
mqtt:
  broker: 192.168.1.215   # ← your P3 IP
  port: 1883
  # No username or password needed
```

Or if you already have a separate HA MQTT broker, configure a second MQTT
integration via the UI (Settings → Devices & Services → MQTT).

### 5. Copy the sensor files

Copy the YAML content into your HA config directory and include in
`configuration.yaml`:

```yaml
# Option A — include each file separately
mqtt: !include ha-config/mqtt_sensors.yaml

# Option B — merge into existing mqtt.yaml
# Append contents of mqtt_sensors.yaml, pylontech_sensors.yaml,
# and alarm_sensors.yaml to your existing mqtt.yaml under sensor:
```

### 6. Replace the Device ID placeholder

Replace every occurrence of `PV001001DEV` with your actual device ID:

```bash
# In the ha-config directory:
sed -i 's/PV001001DEV/YOUR_ACTUAL_ID/g' *.yaml
```

### 7. Add the dashboard

1. In HA go to **Settings → Dashboards → Add Dashboard**
2. Create a new dashboard
3. Click **⋮ → Edit → Raw configuration editor**
4. Paste the contents of `dashboard.yaml`

### 8. Restart Home Assistant

---

## File Reference

| File | Contents |
|------|----------|
| `mqtt_sensors.yaml` | Core sensors: battery SoC, voltage, power, grid, FFR CT, schedule |
| `pylontech_sensors.yaml` | Battery health: SOH, cycles, cell voltages, temperatures, BMS limits |
| `alarm_sensors.yaml` | Individual alarm binary-style sensors + warnings summary |
| `dashboard.yaml` | Lovelace dashboard: power flow, battery health, history |

---

## Alternative: Python MQTT Bridge (Auto-Discovery)

If you prefer not to manage YAML files, the `pv3_monitor/` Python bridge
auto-discovers all sensors in Home Assistant without any manual configuration.
See the main [README](../README.md) for setup instructions.

---

## Control

> **Important**: Publishing to the P3's MQTT broker does **not** control the
> inverter. The M4 ignores locally published messages.

Control is possible via two paths (see [README](../README.md)):

1. **SSH schedule control** — write schedule files to the M4 filesystem
2. **P18 direct serial** — send commands directly to the inverter via RS-232
