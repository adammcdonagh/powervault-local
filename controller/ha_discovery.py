"""Home Assistant MQTT auto-discovery for the standalone Powervault controller.

Published once on startup so HA automatically creates all entities without
any manual YAML configuration.

Discovery topic pattern:
  homeassistant/<component>/<device_id>/<object_id>/config

Each entity also carries an ``availability_topic`` so HA marks it unavailable
when the controller process is offline (using MQTT Last Will and Testament).

References:
  https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Charge mode options
# ---------------------------------------------------------------------------

CHARGE_MODES = ["idle", "charge", "discharge", "force_charge", "force_discharge"]


# ---------------------------------------------------------------------------
# Internal sensor definition table
# ---------------------------------------------------------------------------

@dataclass
class _SensorDef:
    name: str
    friendly_name: str
    unit: str = ""
    device_class: str = ""
    state_class: str = ""
    icon: str = ""
    component: str = "sensor"  # "sensor" or "binary_sensor"


_SENSOR_DEFS: list[_SensorDef] = [
    # --- BMS (Pylontech) ---
    _SensorDef("battery_soc", "Battery SoC", "%", "battery", "measurement"),
    _SensorDef("battery_soh", "Battery SoH (avg)", "%", "", "measurement", "mdi:battery-heart"),
    _SensorDef("battery_soh_min", "Battery SoH (min)", "%", "", "measurement", "mdi:battery-heart-variant"),
    _SensorDef("battery_cycle_count", "Battery Cycle Count", "", "", "total_increasing", "mdi:battery-sync"),
    _SensorDef("battery_voltage", "Battery Voltage", "V", "voltage", "measurement"),
    _SensorDef("battery_current", "Battery Current", "A", "current", "measurement"),
    _SensorDef("battery_power", "Battery Power", "W", "power", "measurement"),
    _SensorDef("battery_cell_voltage_max", "Cell Voltage (max)", "V", "voltage", "measurement"),
    _SensorDef("battery_cell_voltage_min", "Cell Voltage (min)", "V", "voltage", "measurement"),
    _SensorDef("battery_cell_temp_avg", "Cell Temperature (avg)", "°C", "temperature", "measurement"),
    _SensorDef("battery_cell_temp_max", "Cell Temperature (max)", "°C", "temperature", "measurement"),
    _SensorDef("battery_cell_temp_min", "Cell Temperature (min)", "°C", "temperature", "measurement"),
    _SensorDef("bms_charge_current_limit", "BMS Charge Current Limit", "A", "current", "measurement"),
    _SensorDef("bms_discharge_current_limit", "BMS Discharge Current Limit", "A", "current", "measurement"),
    _SensorDef("bms_charge_voltage_limit", "BMS Charge Voltage Limit", "V", "voltage", "measurement"),
    _SensorDef("bms_discharge_voltage_limit", "BMS Discharge Voltage Limit", "V", "voltage", "measurement"),

    # --- ABB PV inverter ---
    _SensorDef("pv_ac_power", "PV AC Power", "W", "power", "measurement"),
    _SensorDef("pv_dc_power", "PV DC Power", "W", "power", "measurement"),
    _SensorDef("pv_dc_voltage", "PV DC Voltage", "V", "voltage", "measurement"),
    _SensorDef("pv_dc_current", "PV DC Current", "A", "current", "measurement"),
    _SensorDef("pv_grid_voltage", "PV Grid Voltage", "V", "voltage", "measurement"),
    _SensorDef("pv_grid_frequency", "PV Grid Frequency", "Hz", "frequency", "measurement"),
    _SensorDef("pv_temperature", "PV Inverter Temperature", "°C", "temperature", "measurement"),
    _SensorDef("pv_energy_today", "PV Energy Today", "kWh", "energy", "total_increasing"),
    _SensorDef("pv_energy_total", "PV Energy Total", "kWh", "energy", "total_increasing"),
    _SensorDef("pv_state", "PV Inverter State", "", "", "", "mdi:solar-panel"),

    # --- Binary sensors ---
    _SensorDef(
        "pv_producing",
        "PV Producing",
        "",
        "power",
        "",
        "mdi:solar-power",
        component="binary_sensor",
    ),
]


# ---------------------------------------------------------------------------
# Topic helpers (single source of truth)
# ---------------------------------------------------------------------------

def sensor_state_topic(device_id: str, name: str) -> str:
    """Canonical state topic for a named sensor."""
    return f"powervault/{device_id}/sensor/{name}/state"


def charge_mode_command_topic(device_id: str) -> str:
    """HA publishes the desired charge mode here."""
    return f"powervault/{device_id}/control/charge_mode/set"


def charge_mode_state_topic(device_id: str) -> str:
    """Controller publishes the current charge mode here."""
    return f"powervault/{device_id}/control/charge_mode/state"


def availability_topic(device_id: str) -> str:
    """Controller publishes 'online'/'offline' here (also used as LWT)."""
    return f"powervault/{device_id}/availability"


# ---------------------------------------------------------------------------
# Device info block (shared by all entities)
# ---------------------------------------------------------------------------

def make_device_info(device_id: str) -> dict[str, Any]:
    """Return the HA MQTT device block for the Powervault controller."""
    return {
        "identifiers": [f"powervault_controller_{device_id}"],
        "name": "Powervault",
        "model": "P3 Local Controller",
        "manufacturer": "Powervault / Iconica",
    }


# ---------------------------------------------------------------------------
# Discovery payload builders
# ---------------------------------------------------------------------------

def _sensor_discovery(defn: _SensorDef, device_id: str) -> tuple[str, str]:
    """Build a single sensor/binary_sensor discovery payload."""
    unique_id = f"powervault_{device_id}_{defn.name}"
    object_id = f"powervault_{defn.name}"

    payload: dict[str, Any] = {
        "name": defn.friendly_name,
        "unique_id": unique_id,
        "object_id": object_id,
        "state_topic": sensor_state_topic(device_id, defn.name),
        "availability_topic": availability_topic(device_id),
        "device": make_device_info(device_id),
    }

    if defn.unit:
        payload["unit_of_measurement"] = defn.unit
    if defn.device_class:
        payload["device_class"] = defn.device_class
    if defn.state_class:
        payload["state_class"] = defn.state_class
    if defn.icon:
        payload["icon"] = defn.icon

    discovery_topic = (
        f"homeassistant/{defn.component}/{device_id}/{object_id}/config"
    )
    return discovery_topic, json.dumps(payload)


def charge_mode_select_discovery(device_id: str) -> tuple[str, str]:
    """Build the HA MQTT *select* discovery payload for charge mode control.

    The select entity lets the user pick a charge mode in HA's UI.
    The selected value is published as a plain string to the command topic;
    the controller reflects the applied mode on the state topic.
    """
    unique_id = f"powervault_{device_id}_charge_mode"
    object_id = "powervault_charge_mode"

    payload: dict[str, Any] = {
        "name": "Charge Mode",
        "unique_id": unique_id,
        "object_id": object_id,
        "options": CHARGE_MODES,
        "command_topic": charge_mode_command_topic(device_id),
        "state_topic": charge_mode_state_topic(device_id),
        "availability_topic": availability_topic(device_id),
        "icon": "mdi:battery-charging",
        "device": make_device_info(device_id),
    }

    discovery_topic = f"homeassistant/select/{device_id}/{object_id}/config"
    return discovery_topic, json.dumps(payload)


def all_discovery_configs(device_id: str) -> list[tuple[str, str]]:
    """Return all ``(discovery_topic, json_payload)`` tuples.

    Publish all of these (retained) on controller startup so that HA
    creates the full set of entities automatically.
    """
    configs: list[tuple[str, str]] = [
        _sensor_discovery(d, device_id) for d in _SENSOR_DEFS
    ]
    configs.append(charge_mode_select_discovery(device_id))
    return configs
