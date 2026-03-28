"""
Home Assistant MQTT auto-discovery helpers.

Publishes discovery config messages so HA automatically creates sensor entities
without any manual YAML configuration.

Discovery topic pattern: ``homeassistant/<component>/<device_id>/<object_id>/config``

References:
  https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
"""

from __future__ import annotations

import json
from typing import Any

from .topics import SensorReading


# ---------------------------------------------------------------------------
# Device info (shared across all entities)
# ---------------------------------------------------------------------------

def make_device_info(device_id: str) -> dict[str, Any]:
    """Return the HA MQTT device block for the Powervault P3."""
    return {
        "identifiers": [f"powervault_p3_{device_id}"],
        "name": "Powervault P3",
        "model": "P3",
        "manufacturer": "Powervault / Iconica",
        "hw_version": "M4 Controller",
    }


# ---------------------------------------------------------------------------
# Sensor discovery
# ---------------------------------------------------------------------------

def sensor_config(
    reading: SensorReading,
    device_id: str,
    state_topic: str,
    value_template: str = "{{ value_json }}",
) -> tuple[str, str]:
    """Build an HA MQTT sensor discovery payload.

    Returns ``(discovery_topic, json_payload)`` ready to publish.
    """
    unique_id = f"pv3_{device_id}_{reading.name}"
    object_id = f"pv3_{reading.name}"

    payload: dict[str, Any] = {
        "name": reading.friendly_name or reading.name.replace("_", " ").title(),
        "unique_id": unique_id,
        "object_id": object_id,
        "state_topic": state_topic,
        "value_template": value_template,
        "device": make_device_info(device_id),
    }

    if reading.unit:
        payload["unit_of_measurement"] = reading.unit
    if reading.device_class:
        payload["device_class"] = reading.device_class
    if reading.state_class:
        payload["state_class"] = reading.state_class
    if reading.icon:
        payload["icon"] = reading.icon

    discovery_topic = f"homeassistant/sensor/{device_id}/{object_id}/config"
    return discovery_topic, json.dumps(payload)


# ---------------------------------------------------------------------------
# Select entity (schedule mode control)
# ---------------------------------------------------------------------------

SCHEDULE_OPTIONS = ["Idle", "Charge", "Discharge", "Force Charge", "Force Discharge"]


def schedule_select_config(
    device_id: str,
    command_topic: str,
    state_topic: str,
) -> tuple[str, str]:
    """Build an HA MQTT *select* discovery payload for the schedule mode.

    The selected option is published as a plain string to *command_topic*.
    The current state is read from *state_topic* (also a plain string).
    """
    unique_id = f"pv3_{device_id}_schedule_mode"
    object_id = f"pv3_schedule_mode"

    payload: dict[str, Any] = {
        "name": "Schedule Mode",
        "unique_id": unique_id,
        "object_id": object_id,
        "options": SCHEDULE_OPTIONS,
        "command_topic": command_topic,
        "state_topic": state_topic,
        "icon": "mdi:calendar-clock",
        "device": make_device_info(device_id),
    }

    discovery_topic = f"homeassistant/select/{device_id}/{object_id}/config"
    return discovery_topic, json.dumps(payload)


def setpoint_number_config(
    device_id: str,
    command_topic: str,
    state_topic: str,
) -> tuple[str, str]:
    """Build an HA MQTT *number* discovery payload for the power setpoint (W)."""
    unique_id = f"pv3_{device_id}_schedule_setpoint"
    object_id = f"pv3_schedule_setpoint_control"

    payload: dict[str, Any] = {
        "name": "Schedule Setpoint",
        "unique_id": unique_id,
        "object_id": object_id,
        "command_topic": command_topic,
        "state_topic": state_topic,
        "unit_of_measurement": "W",
        "device_class": "power",
        "min": 0,
        "max": 5500,
        "step": 100,
        "icon": "mdi:lightning-bolt",
        "device": make_device_info(device_id),
    }

    discovery_topic = f"homeassistant/number/{device_id}/{object_id}/config"
    return discovery_topic, json.dumps(payload)


# ---------------------------------------------------------------------------
# Helpers for building per-sensor state topics and value templates
# ---------------------------------------------------------------------------

def state_topic_for(device_id: str, name: str) -> str:
    """Canonical HA MQTT state topic for a sensor named *name*."""
    return f"powervault/{device_id}/sensor/{name}/state"


def control_topic_for(device_id: str, control: str) -> str:
    """Canonical HA MQTT command topic for a control named *control*."""
    return f"powervault/{device_id}/control/{control}"
