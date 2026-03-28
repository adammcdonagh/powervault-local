"""
P3 MQTT topic parsing.

The Powervault P3 M4 controller publishes to these topics (all read-only):

  pv/PV3/<DEVICE_ID>/bms/soc
  pv/PV3/<DEVICE_ID>/inverter/measurements
  pv/PV3/<DEVICE_ID>/inverter/alarms
  pv/PV3/<DEVICE_ID>/inverter/charge
  pv/PV3/<DEVICE_ID>/pylontech/info
  pv/PV3/<DEVICE_ID>/ffr/measurements
  pv/PV3/<DEVICE_ID>/schedule/event
  pv/PV3/<DEVICE_ID>/eps/status
  pv/PV3/<DEVICE_ID>/eps_schedule/event
  pv/PV3/<DEVICE_ID>/m4/maxpower
  pv/PV3/<DEVICE_ID>/ffrcontroller/state
  pv/PV3/<DEVICE_ID>/safetycheck/state
  pv/PV3/<DEVICE_ID>/m4/pylontech_alerts

All measurements with raw integer values use SI-prefixed units as documented:
  - Voltages:     millivolts  (÷ 1000 → V)
  - Currents:     milliamps   (÷ 1000 → A)
  - Power:        milliwatts  (÷ 1000 → W)
  - Temperatures: milli°C     (÷ 1000 → °C)
  - SOC:          centi-%     (÷ 100  → %)

Sources: confirmed against live P3 unit P3-1164 (December 2024/2025).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SensorReading:
    """A normalised sensor reading extracted from a P3 MQTT payload."""

    name: str
    """Stable machine name used as the HA entity unique_id suffix."""

    value: float
    """Normalised value in the unit described by *unit*."""

    unit: str
    """Physical unit string (e.g. "%", "V", "W", "°C")."""

    device_class: str | None = None
    """Home Assistant device class (e.g. "battery", "voltage", "power")."""

    state_class: str = "measurement"
    """Home Assistant state_class."""

    icon: str | None = None
    """MDI icon override (e.g. "mdi:battery")."""

    friendly_name: str | None = None
    """Human-readable label used in HA."""


# ---------------------------------------------------------------------------
# Schedule event codes
# ---------------------------------------------------------------------------

SCHEDULE_EVENT_NAMES: dict[int, str] = {
    0: "Idle",
    1: "Charge",
    2: "Discharge",
    3: "Force Charge",
    4: "Force Discharge",
}


# ---------------------------------------------------------------------------
# Topic parsers
# ---------------------------------------------------------------------------

def parse_bms_soc(payload: list | dict) -> list[SensorReading]:
    """Parse ``bms/soc`` payloads.

    Two formats are published by the M4:

    * List format (aggregate): ``[{"measurement": "StateOfCharge", "value": 9700}]``
      where the raw value is in centi-% (9700 → 97.00 %).
    * Dict format (per-module): ``{"module_addr": 4, "StateOfCharge": 8200}``
      where the raw value is also in centi-%.
    """
    readings: list[SensorReading] = []

    if isinstance(payload, list):
        for item in payload:
            if item.get("measurement") == "StateOfCharge":
                raw = item.get("value")
                if raw is not None:
                    readings.append(SensorReading(
                        name="soc",
                        value=raw / 100.0,
                        unit="%",
                        device_class="battery",
                        friendly_name="Battery State of Charge",
                    ))
    elif isinstance(payload, dict):
        raw = payload.get("StateOfCharge") or payload.get("usable_soc")
        if raw is not None:
            readings.append(SensorReading(
                name="soc",
                value=raw / 100.0,
                unit="%",
                device_class="battery",
                friendly_name="Battery State of Charge",
            ))

    return readings


def parse_inverter_measurements(payload: list[dict]) -> list[SensorReading]:
    """Parse ``inverter/measurements`` payloads.

    Each element has the shape::

        {"channel": "BATTERY", "measurement": "Voltage", "type": "Ac", "value": 49800}

    Raw values are in mV / mA / mW.
    """
    readings: list[SensorReading] = []

    for item in payload:
        channel = item.get("channel", "")
        measurement = item.get("measurement", "")
        mtype = item.get("type", "")
        raw = item.get("value")

        if raw is None:
            continue

        if channel == "BATTERY":
            if measurement == "Voltage":
                readings.append(SensorReading(
                    name="battery_voltage",
                    value=raw / 1000.0,
                    unit="V",
                    device_class="voltage",
                    friendly_name="Battery Voltage",
                ))
            elif measurement == "ChargeCurrent":
                readings.append(SensorReading(
                    name="battery_current",
                    value=raw / 1000.0,
                    unit="A",
                    device_class="current",
                    friendly_name="Battery Charge Current (inverter)",
                ))
            elif measurement == "Capacity":
                readings.append(SensorReading(
                    name="battery_capacity",
                    value=float(raw),
                    unit="%",
                    device_class="battery",
                    friendly_name="Battery Capacity",
                ))

        elif channel == "GRID":
            if measurement == "Voltage" and mtype == "Ac":
                readings.append(SensorReading(
                    name="grid_voltage",
                    value=raw / 1000.0,
                    unit="V",
                    device_class="voltage",
                    friendly_name="Grid Voltage",
                ))
            elif measurement == "Frequency":
                readings.append(SensorReading(
                    name="grid_frequency",
                    value=raw / 1000.0,
                    unit="Hz",
                    device_class="frequency",
                    friendly_name="Grid Frequency",
                ))
            elif measurement == "Power" and mtype == "Active":
                readings.append(SensorReading(
                    name="grid_power_inverter",
                    value=raw / 1000.0,
                    unit="W",
                    device_class="power",
                    friendly_name="Grid Active Power (inverter)",
                ))

        elif channel == "OUTPUT":
            if measurement == "Power" and mtype == "Active":
                readings.append(SensorReading(
                    name="output_active_power",
                    value=raw / 1000.0,
                    unit="W",
                    device_class="power",
                    friendly_name="AC Output Active Power",
                ))
            elif measurement == "Power" and mtype == "Apparent":
                readings.append(SensorReading(
                    name="output_apparent_power",
                    value=raw / 1000.0,
                    unit="VA",
                    device_class="apparent_power",
                    friendly_name="AC Output Apparent Power",
                ))
            elif measurement == "Voltage":
                readings.append(SensorReading(
                    name="output_voltage",
                    value=raw / 1000.0,
                    unit="V",
                    device_class="voltage",
                    friendly_name="AC Output Voltage",
                ))
            elif measurement == "Frequency":
                readings.append(SensorReading(
                    name="output_frequency",
                    value=raw / 1000.0,
                    unit="Hz",
                    device_class="frequency",
                    friendly_name="AC Output Frequency",
                ))

    return readings


def parse_inverter_charge(payload: dict) -> list[SensorReading]:
    """Parse ``inverter/charge`` payloads.

    Format: ``{"power": 591}`` — positive = discharging, negative = charging.
    """
    readings: list[SensorReading] = []
    power = payload.get("power")
    if power is not None:
        readings.append(SensorReading(
            name="battery_power",
            value=float(power),
            unit="W",
            device_class="power",
            friendly_name="Battery Power (+ discharge / − charge)",
            icon="mdi:battery-charging",
        ))
    return readings


def parse_inverter_alarms(payload: dict) -> list[SensorReading]:
    """Parse ``inverter/alarms`` payloads.

    The payload is a flat dict mixing string-encoded bit flags ("0"/"1") with
    numeric temperature fields::

        {
            "fan_lock": "0",
            "battery_low": "0",
            "inverter_temperature": 42.5,
            ...
        }

    Returns one reading per numeric temperature field; the alarm bits
    are returned as a single ``alarm_count`` reading and a separate
    ``alarm_active`` boolean-style reading per bit.
    """
    readings: list[SensorReading] = []
    temperature_keys = {"inverter_temperature", "boost_temperature", "inner_temperature"}

    for key, val in payload.items():
        if key in temperature_keys and val is not None:
            try:
                readings.append(SensorReading(
                    name=key,
                    value=float(val),
                    unit="°C",
                    device_class="temperature",
                    friendly_name=key.replace("_", " ").title(),
                ))
            except (TypeError, ValueError):
                pass

    # Count active alarm bits
    alarm_count = sum(
        1 for k, v in payload.items()
        if k not in temperature_keys and k != "timestamp" and k != "warnings_summary" and v == "1"
    )
    readings.append(SensorReading(
        name="alarm_count",
        value=float(alarm_count),
        unit="",
        state_class="measurement",
        icon="mdi:alert-circle",
        friendly_name="Active Alarm Count",
    ))

    return readings


def parse_pylontech_info(payload: list[dict]) -> list[SensorReading]:
    """Parse ``pylontech/info`` payloads.

    Each message is a single-element list with one measurement::

        [{"measurement": "StateOfHealth", "type": "Avg", "value": 92}]

    Values use milli-units where noted:
      - CellTemperature / BMSTemperature: milli°C (÷1000)
      - ModuleVoltage: mV (÷1000)
      - Current: mA (÷1000, signed — negative = discharging)
      - ChargeVoltageLimit / DischargeVoltageLimit: mV (÷1000)
      - ChargeCurrentLimit / DischargeCurrentLimit: mA (÷1000)
    """
    readings: list[SensorReading] = []

    for item in payload:
        m = item.get("measurement", "")
        t = item.get("type", "")
        raw = item.get("value")
        if raw is None:
            continue

        if m == "StateOfHealth":
            readings.append(SensorReading(
                name=f"soh_{t.lower()}",
                value=float(raw),
                unit="%",
                icon="mdi:battery-heart-variant",
                friendly_name=f"Battery State of Health ({t})",
            ))

        elif m == "CycleNumber":
            readings.append(SensorReading(
                name=f"cycle_count_{t.lower()}",
                value=float(raw),
                unit="",
                state_class="total_increasing",
                icon="mdi:battery-sync",
                friendly_name=f"Battery Cycle Count ({t})",
            ))

        elif m == "CellVoltage":
            readings.append(SensorReading(
                name=f"cell_voltage_{t.lower()}",
                value=float(raw),
                unit="mV",
                device_class="voltage",
                friendly_name=f"Cell Voltage ({t})",
            ))

        elif m == "CellTemperature":
            readings.append(SensorReading(
                name=f"cell_temp_{t.lower()}",
                value=raw / 1000.0,
                unit="°C",
                device_class="temperature",
                friendly_name=f"Cell Temperature ({t})",
            ))

        elif m == "BMSTemperature":
            readings.append(SensorReading(
                name=f"bms_temp_{t.lower()}",
                value=raw / 1000.0,
                unit="°C",
                device_class="temperature",
                friendly_name=f"BMS Temperature ({t})",
            ))

        elif m == "ModuleVoltage" and t == "Avg":
            readings.append(SensorReading(
                name="module_voltage_avg",
                value=raw / 1000.0,
                unit="V",
                device_class="voltage",
                friendly_name="Battery Module Voltage (avg)",
            ))

        elif m == "Current" and t == "Total":
            readings.append(SensorReading(
                name="battery_current_total",
                value=raw / 1000.0,
                unit="A",
                device_class="current",
                friendly_name="Battery Current Total (BMS)",
            ))

        elif m == "ChargeVoltageLimit":
            readings.append(SensorReading(
                name="charge_voltage_limit",
                value=raw / 1000.0,
                unit="V",
                device_class="voltage",
                friendly_name="Charge Voltage Limit",
            ))

        elif m == "DischargeVoltageLimit":
            readings.append(SensorReading(
                name="discharge_voltage_limit",
                value=raw / 1000.0,
                unit="V",
                device_class="voltage",
                friendly_name="Discharge Voltage Limit",
            ))

        elif m == "ChargeCurrentLimit":
            readings.append(SensorReading(
                name="charge_current_limit",
                value=raw / 1000.0,
                unit="A",
                device_class="current",
                friendly_name="Charge Current Limit",
            ))

        elif m == "DischargeCurrentLimit":
            readings.append(SensorReading(
                name="discharge_current_limit",
                value=abs(raw / 1000.0),
                unit="A",
                device_class="current",
                friendly_name="Discharge Current Limit",
            ))

    return readings


def parse_ffr_measurements(payload: list[dict]) -> list[SensorReading]:
    """Parse ``ffr/measurements`` payloads (FFR CT clamp readings).

    Each element::

        {"channel": "LOCAL", "measurement": "Power", "type": "Active", "value": 11736}

    Raw values in mW (÷1000 → W).

    **Important**: The CT clamp labelling on the M4 board is swapped relative
    to the HA/dashboard naming used in the reference implementation:
      - ``LOCAL`` channel → house consumption power
      - ``HOUSE`` channel → grid power  (positive = import, negative = export)
      - ``AUX1``  channel → auxiliary circuit power
    """
    readings: list[SensorReading] = []

    channel_map = {
        "LOCAL": ("house_power", "House Consumption Power"),
        "HOUSE": ("grid_power", "Grid Power (+ import / − export)"),
        "AUX1":  ("aux_power",  "AUX1 Power"),
    }

    for item in payload:
        channel = item.get("channel", "")
        measurement = item.get("measurement", "")
        mtype = item.get("type", "")
        raw = item.get("value")

        if raw is None or measurement != "Power" or mtype != "Active":
            continue

        if channel in channel_map:
            name, label = channel_map[channel]
            readings.append(SensorReading(
                name=name,
                value=raw / 1000.0,
                unit="W",
                device_class="power",
                friendly_name=label,
            ))

    return readings


def parse_schedule_event(payload: dict) -> list[SensorReading]:
    """Parse ``schedule/event`` payloads.

    Format: ``{"time": "2025-12-18T07:46:49", "event": 0, "setpoint": 0}``
    """
    readings: list[SensorReading] = []

    event = payload.get("event")
    setpoint = payload.get("setpoint")

    if event is not None:
        readings.append(SensorReading(
            name="schedule_event",
            value=float(event),
            unit="",
            icon="mdi:calendar-clock",
            friendly_name="Schedule Event Code",
        ))

    if setpoint is not None:
        readings.append(SensorReading(
            name="schedule_setpoint",
            value=float(setpoint),
            unit="W",
            device_class="power",
            friendly_name="Schedule Setpoint",
        ))

    return readings


def parse_maxpower(payload: dict | list) -> list[SensorReading]:
    """Parse ``m4/maxpower`` payloads.

    Format: ``[{"ChgPower": 4792, "DchgPower": -6750}]`` or dict variant.
    """
    readings: list[SensorReading] = []

    if isinstance(payload, list):
        payload = payload[0] if payload else {}

    chg = payload.get("ChgPower")
    dchg = payload.get("DchgPower")

    if chg is not None:
        readings.append(SensorReading(
            name="max_charge_power",
            value=float(chg),
            unit="W",
            device_class="power",
            friendly_name="Max Charge Power",
        ))
    if dchg is not None:
        readings.append(SensorReading(
            name="max_discharge_power",
            value=float(dchg),
            unit="W",
            device_class="power",
            friendly_name="Max Discharge Power",
        ))

    return readings


def parse_eps_status(payload: dict | list) -> list[SensorReading]:
    """Parse ``eps/status`` payloads.

    Format: ``{"Reserve": 20, "Mode": 0}``
    """
    readings: list[SensorReading] = []

    if isinstance(payload, list):
        payload = payload[0] if payload else {}

    reserve = payload.get("Reserve")
    mode = payload.get("Mode")

    if reserve is not None:
        readings.append(SensorReading(
            name="eps_reserve",
            value=float(reserve),
            unit="%",
            icon="mdi:battery-lock",
            friendly_name="EPS Reserve SoC",
        ))
    if mode is not None:
        readings.append(SensorReading(
            name="eps_mode",
            value=float(mode),
            unit="",
            icon="mdi:power-standby",
            friendly_name="EPS Mode",
        ))

    return readings


# ---------------------------------------------------------------------------
# Topic router
# ---------------------------------------------------------------------------

_PARSERS: dict[str, Any] = {
    "bms/soc":                 parse_bms_soc,
    "inverter/measurements":   parse_inverter_measurements,
    "inverter/alarms":         parse_inverter_alarms,
    "inverter/charge":         parse_inverter_charge,
    "pylontech/info":          parse_pylontech_info,
    "ffr/measurements":        parse_ffr_measurements,
    "schedule/event":          parse_schedule_event,
    "m4/maxpower":             parse_maxpower,
    "eps/status":              parse_eps_status,
}


def parse_topic(topic_suffix: str, payload: Any) -> list[SensorReading]:
    """Route a P3 MQTT *topic_suffix* and *payload* to the correct parser.

    *topic_suffix* is the part after ``pv/PV3/<DEVICE_ID>/``.
    Returns an empty list for topics that are not currently parsed.
    """
    parser = _PARSERS.get(topic_suffix)
    if parser is None:
        return []
    return parser(payload)
