"""Standalone M4-bypass controller for the Powervault P3.

This is the entry point for the fully independent local control stack.
It replaces the dependency on the M4 controller's MQTT broker by talking
directly to the hardware:

  Pylontech batteries  ← RS485   → /dev/ttyUSB1 (python-pylontech)
  Iconica inverter     ← USB/RS232 → /dev/ttyUSB0 (p18_serial)
  ABB UNO PVI 3.0      ← RS485   → /dev/ttyUSB2 (aurorapy) [optional]

The controller loop:
  1. Polls the Pylontech BMS for SoC, voltage, current, health and limits
  2. Polls the ABB PV inverter for solar production (if configured)
  3. Applies BMS limits to the Iconica inverter
  4. Decides charge/discharge mode via the scheduler
  5. Issues the appropriate P18 command to the inverter
  6. Publishes all sensor readings to HA via MQTT (auto-discovery)

Run directly:
  python -m controller.main

Or via docker-compose with the ``controller`` service.

Environment variables
---------------------
All hardware ports and MQTT settings are read from environment variables
(see .env.example for the full list).

Required:
  INVERTER_PORT         Serial/USB port for the Iconica inverter
  BATTERY_PORT          RS485 port for the Pylontech batteries
  NUM_BATTERY_MODULES   Number of Pylontech modules in the stack

  HA_MQTT_HOST          Home Assistant MQTT broker hostname
  HA_MQTT_PORT          HA broker port (default 1883)
  HA_MQTT_USER          HA broker username (optional)
  HA_MQTT_PASS          HA broker password (optional)

Optional:
  PV_INVERTER_PORT      RS485 port for the ABB Aurora PV inverter
                        (leave unset to disable PV inverter polling)
  PV_INVERTER_ADDRESS   Aurora RS485 address (default 2)
  DEVICE_ID             Identifier used in MQTT topics (default "powervault")
  POLL_INTERVAL         Seconds between polling cycles (default 30)
  INVERTER_IS_USB       Set to "true" if using USB HID port (default false)
"""

from __future__ import annotations

import logging
import os
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from pylontech_driver.bms import BmsPoller, BmsSnapshot
from abb_aurora.aurora import AbbAuroraPoller, AuroraSnapshot
from p18_serial.p18 import P18Inverter
from controller.safety import SafetyError, apply_bms_limits, check_charge_allowed, check_discharge_allowed
from controller.scheduler import decide_mode

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

def _build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    user = _env("HA_MQTT_USER")
    password = _env("HA_MQTT_PASS")
    if user:
        client.username_pw_set(user, password or None)
    client.connect(
        _env("HA_MQTT_HOST", "localhost"),
        _env_int("HA_MQTT_PORT", 1883),
    )
    client.loop_start()
    return client


def _topic(device_id: str, name: str) -> str:
    return f"powervault/{device_id}/sensor/{name}/state"


def _publish_bms(client: mqtt.Client, device_id: str, snap: BmsSnapshot) -> None:
    """Publish all BMS sensor readings to HA MQTT."""
    readings: dict[str, object] = {
        "battery_soc": round(snap.soc_pct, 1),
        "battery_soh": round(snap.soh_pct, 1),
        "battery_soh_min": round(snap.soh_pct_min, 1),
        "battery_cycle_count": int(snap.cycle_count_max),
        "battery_voltage": round(snap.battery_voltage_v, 3),
        "battery_current": round(snap.battery_current_a, 3),
        "battery_power": round(snap.battery_power_w, 1),
        "battery_cell_voltage_max": round(snap.cell_voltage_max_v, 4),
        "battery_cell_voltage_min": round(snap.cell_voltage_min_v, 4),
        "battery_cell_temp_avg": round(snap.cell_temp_avg_c, 1),
        "battery_cell_temp_max": round(snap.cell_temp_max_c, 1),
        "battery_cell_temp_min": round(snap.cell_temp_min_c, 1),
    }
    if snap.charge_current_limit_a is not None:
        readings["bms_charge_current_limit"] = round(snap.charge_current_limit_a, 1)
    if snap.discharge_current_limit_a is not None:
        readings["bms_discharge_current_limit"] = round(snap.discharge_current_limit_a, 1)
    if snap.charge_voltage_limit_v is not None:
        readings["bms_charge_voltage_limit"] = round(snap.charge_voltage_limit_v, 2)
    if snap.discharge_voltage_limit_v is not None:
        readings["bms_discharge_voltage_limit"] = round(snap.discharge_voltage_limit_v, 2)

    for name, value in readings.items():
        client.publish(_topic(device_id, name), str(value), retain=True)


def _publish_pv(client: mqtt.Client, device_id: str, snap: AuroraSnapshot) -> None:
    """Publish ABB PV inverter readings to HA MQTT."""
    readings: dict[str, object] = {
        "pv_ac_power": round(snap.ac_power_w, 1),
        "pv_dc_power": round(snap.dc_power_w, 1),
        "pv_producing": "ON" if snap.is_producing else "OFF",
    }
    if snap.dc_voltage_v is not None:
        readings["pv_dc_voltage"] = round(snap.dc_voltage_v, 2)
    if snap.dc_current_a is not None:
        readings["pv_dc_current"] = round(snap.dc_current_a, 3)
    if snap.grid_voltage_v is not None:
        readings["pv_grid_voltage"] = round(snap.grid_voltage_v, 2)
    if snap.grid_frequency_hz is not None:
        readings["pv_grid_frequency"] = round(snap.grid_frequency_hz, 2)
    if snap.temperature_c is not None:
        readings["pv_temperature"] = round(snap.temperature_c, 1)
    if snap.energy_today_kwh is not None:
        readings["pv_energy_today"] = round(snap.energy_today_kwh, 3)
    if snap.energy_total_kwh is not None:
        readings["pv_energy_total"] = round(snap.energy_total_kwh, 3)
    if snap.inverter_state is not None:
        readings["pv_state"] = snap.inverter_state

    for name, value in readings.items():
        client.publish(_topic(device_id, name), str(value), retain=True)


def _publish_mode(client: mqtt.Client, device_id: str, mode: str) -> None:
    client.publish(_topic(device_id, "battery_mode"), mode, retain=True)


# ---------------------------------------------------------------------------
# Main control loop
# ---------------------------------------------------------------------------

def _apply_mode(inverter: P18Inverter, bms: BmsSnapshot, mode: str) -> None:
    """Apply the scheduler-decided mode to the inverter, with safety checks."""
    try:
        apply_bms_limits(inverter, bms)
    except SafetyError as exc:
        logger.error("BMS limit application failed: %s — going idle", exc)
        inverter.idle_mode()
        return

    try:
        if mode == "charge":
            check_charge_allowed(bms)
            inverter.force_charge()
        elif mode == "discharge":
            check_discharge_allowed(bms)
            inverter.normal_mode()
        elif mode == "force_charge":
            inverter.force_charge()
        elif mode == "force_discharge":
            check_discharge_allowed(bms)
            inverter.normal_mode()
        else:  # idle
            inverter.idle_mode()
        logger.info("Inverter mode set: %s", mode)
    except SafetyError as exc:
        logger.warning("Mode %r blocked by safety: %s — going idle", mode, exc)
        inverter.idle_mode()
    except Exception as exc:
        logger.error("Failed to apply mode %r to inverter: %s", mode, exc)


def run() -> None:
    """Main polling and control loop. Runs indefinitely."""
    device_id = _env("DEVICE_ID", "powervault")
    poll_interval = _env_int("POLL_INTERVAL", 30)

    inverter_port = _env("INVERTER_PORT", "/dev/ttyUSB0")
    is_usb = _env_bool("INVERTER_IS_USB", False)

    battery_port = _env("BATTERY_PORT", "/dev/ttyUSB1")
    num_modules = _env_int("NUM_BATTERY_MODULES", 1)

    pv_port = _env("PV_INVERTER_PORT", "")
    pv_address = _env_int("PV_INVERTER_ADDRESS", 2)

    logger.info("Starting M4-bypass controller (device_id=%s)", device_id)
    logger.info("  Inverter:  %s (usb=%s)", inverter_port, is_usb)
    logger.info("  Batteries: %s (%d modules)", battery_port, num_modules)
    if pv_port:
        logger.info("  PV inverter: %s (address %d)", pv_port, pv_address)
    else:
        logger.info("  PV inverter: disabled (set PV_INVERTER_PORT to enable)")

    mqtt_client = _build_mqtt_client()

    bms_poller = BmsPoller(port=battery_port, num_modules=num_modules)
    pv_poller = AbbAuroraPoller(port=pv_port, address=pv_address) if pv_port else None

    try:
        with P18Inverter(port=inverter_port, is_usb=is_usb) as inverter:
            logger.info("Inverter connected")

            while True:
                loop_start = time.monotonic()

                # --- 1. Read battery BMS ---
                bms: BmsSnapshot | None = None
                try:
                    bms = bms_poller.read()
                    _publish_bms(mqtt_client, device_id, bms)
                    logger.info(
                        "BMS: SoC=%.1f%% V=%.2fV I=%.1fA",
                        bms.soc_pct,
                        bms.battery_voltage_v,
                        bms.battery_current_a,
                    )
                except Exception as exc:
                    logger.error("BMS poll failed: %s", exc)

                # --- 2. Read PV inverter ---
                pv: AuroraSnapshot | None = None
                if pv_poller is not None:
                    try:
                        pv = pv_poller.read()
                        _publish_pv(mqtt_client, device_id, pv)
                    except Exception as exc:
                        logger.error("PV inverter poll failed: %s", exc)

                # --- 3. Decide and apply mode ---
                if bms is not None:
                    pv_snap = pv if pv is not None else AuroraSnapshot()
                    # House power: we don't have a CT here yet; use battery
                    # discharge power as a proxy until PZEM-004T is wired.
                    # Negative battery power means discharging (supplying house).
                    house_proxy_w = max(0.0, -bms.battery_power_w)

                    mode = decide_mode(bms, pv_snap, house_proxy_w)
                    _apply_mode(inverter, bms, mode)
                    _publish_mode(mqtt_client, device_id, mode)
                else:
                    logger.warning("No BMS data — skipping inverter control this cycle")

                # --- 4. Sleep remainder of poll interval ---
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0.0, poll_interval - elapsed)
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Controller stopped by user")
    finally:
        bms_poller.close()
        if pv_poller is not None:
            pv_poller.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    run()
