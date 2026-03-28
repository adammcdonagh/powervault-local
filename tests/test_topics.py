"""
Tests for pv3_monitor.topics — P3 MQTT payload parsing.

All test data matches the formats confirmed from live P3 unit P3-1164
(as documented in the reference community repository).
"""

import pytest
from pv3_monitor.topics import (
    SCHEDULE_EVENT_NAMES,
    SensorReading,
    parse_bms_soc,
    parse_eps_status,
    parse_ffr_measurements,
    parse_inverter_alarms,
    parse_inverter_charge,
    parse_inverter_measurements,
    parse_maxpower,
    parse_pylontech_info,
    parse_schedule_event,
    parse_topic,
)


# ---------------------------------------------------------------------------
# bms/soc
# ---------------------------------------------------------------------------

class TestParseBmsSoc:
    def test_list_format(self):
        payload = [{"measurement": "StateOfCharge", "value": 9700}]
        readings = parse_bms_soc(payload)
        assert len(readings) == 1
        r = readings[0]
        assert r.name == "soc"
        assert r.value == pytest.approx(97.0)
        assert r.unit == "%"
        assert r.device_class == "battery"

    def test_dict_format(self):
        payload = {"module_addr": 4, "StateOfCharge": 8200}
        readings = parse_bms_soc(payload)
        assert len(readings) == 1
        assert readings[0].value == pytest.approx(82.0)

    def test_list_ignores_other_measurements(self):
        payload = [
            {"measurement": "Voltage", "value": 49800},
            {"measurement": "StateOfCharge", "value": 5000},
        ]
        readings = parse_bms_soc(payload)
        # Only StateOfCharge should be extracted
        assert len(readings) == 1
        assert readings[0].value == pytest.approx(50.0)

    def test_empty_list(self):
        assert parse_bms_soc([]) == []

    def test_empty_dict(self):
        assert parse_bms_soc({}) == []

    def test_missing_value(self):
        payload = [{"measurement": "StateOfCharge"}]
        assert parse_bms_soc(payload) == []


# ---------------------------------------------------------------------------
# inverter/measurements
# ---------------------------------------------------------------------------

class TestParseInverterMeasurements:
    def test_battery_voltage(self):
        payload = [{"channel": "BATTERY", "measurement": "Voltage", "value": 49800}]
        readings = parse_inverter_measurements(payload)
        r = next(r for r in readings if r.name == "battery_voltage")
        assert r.value == pytest.approx(49.8)
        assert r.unit == "V"

    def test_grid_voltage(self):
        payload = [{"channel": "GRID", "measurement": "Voltage", "type": "Ac", "value": 225800}]
        readings = parse_inverter_measurements(payload)
        r = next(r for r in readings if r.name == "grid_voltage")
        assert r.value == pytest.approx(225.8)

    def test_grid_frequency(self):
        payload = [{"channel": "GRID", "measurement": "Frequency", "value": 50000}]
        readings = parse_inverter_measurements(payload)
        r = next(r for r in readings if r.name == "grid_frequency")
        assert r.value == pytest.approx(50.0)
        assert r.unit == "Hz"

    def test_battery_capacity(self):
        payload = [{"channel": "BATTERY", "measurement": "Capacity", "value": 77}]
        readings = parse_inverter_measurements(payload)
        r = next(r for r in readings if r.name == "battery_capacity")
        assert r.value == pytest.approx(77.0)
        assert r.unit == "%"

    def test_skips_null_value(self):
        payload = [{"channel": "BATTERY", "measurement": "Voltage", "value": None}]
        readings = parse_inverter_measurements(payload)
        assert not any(r.name == "battery_voltage" for r in readings)

    def test_unknown_channel_ignored(self):
        payload = [{"channel": "UNKNOWN", "measurement": "Voltage", "value": 1000}]
        assert parse_inverter_measurements(payload) == []


# ---------------------------------------------------------------------------
# inverter/charge
# ---------------------------------------------------------------------------

class TestParseInverterCharge:
    def test_positive_discharge(self):
        readings = parse_inverter_charge({"power": 591})
        assert len(readings) == 1
        assert readings[0].name == "battery_power"
        assert readings[0].value == pytest.approx(591.0)
        assert readings[0].unit == "W"

    def test_negative_charge(self):
        readings = parse_inverter_charge({"power": -1500})
        assert readings[0].value == pytest.approx(-1500.0)

    def test_missing_power_key(self):
        assert parse_inverter_charge({}) == []


# ---------------------------------------------------------------------------
# inverter/alarms
# ---------------------------------------------------------------------------

class TestParseInverterAlarms:
    def test_temperature_extracted(self):
        payload = {"inverter_temperature": 42.5, "fan_lock": "0"}
        readings = parse_inverter_alarms(payload)
        temps = [r for r in readings if r.name == "inverter_temperature"]
        assert len(temps) == 1
        assert temps[0].value == pytest.approx(42.5)
        assert temps[0].unit == "°C"

    def test_alarm_count_zero(self):
        payload = {"fan_lock": "0", "battery_low": "0", "inverter_temperature": 38.0}
        readings = parse_inverter_alarms(payload)
        count = next(r for r in readings if r.name == "alarm_count")
        assert count.value == pytest.approx(0.0)

    def test_alarm_count_nonzero(self):
        payload = {"fan_lock": "1", "battery_low": "1", "overload": "0"}
        readings = parse_inverter_alarms(payload)
        count = next(r for r in readings if r.name == "alarm_count")
        assert count.value == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# pylontech/info
# ---------------------------------------------------------------------------

class TestParsePylontechInfo:
    def test_soh_avg(self):
        payload = [{"measurement": "StateOfHealth", "type": "Avg", "value": 92}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "soh_avg")
        assert r.value == pytest.approx(92.0)
        assert r.unit == "%"

    def test_cell_temp_avg(self):
        # Raw value is milli-°C (20100 → 20.1 °C)
        payload = [{"measurement": "CellTemperature", "type": "Avg", "value": 20100}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "cell_temp_avg")
        assert r.value == pytest.approx(20.1)
        assert r.unit == "°C"

    def test_cell_voltage_max(self):
        # Raw value already in mV (no conversion)
        payload = [{"measurement": "CellVoltage", "type": "Max", "value": 3317}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "cell_voltage_max")
        assert r.value == pytest.approx(3317.0)
        assert r.unit == "mV"

    def test_module_voltage_avg(self):
        # mV → V
        payload = [{"measurement": "ModuleVoltage", "type": "Avg", "value": 49746}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "module_voltage_avg")
        assert r.value == pytest.approx(49.746)

    def test_battery_current_total_negative(self):
        # mA → A; negative = discharging
        payload = [{"measurement": "Current", "type": "Total", "value": -10230}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "battery_current_total")
        assert r.value == pytest.approx(-10.23)

    def test_discharge_current_limit_made_positive(self):
        # DischargeCurrentLimit is typically negative in the raw data → abs()
        payload = [{"measurement": "DischargeCurrentLimit", "type": "", "value": -150000}]
        readings = parse_pylontech_info(payload)
        r = next(r for r in readings if r.name == "discharge_current_limit")
        assert r.value == pytest.approx(150.0)
        assert r.value >= 0


# ---------------------------------------------------------------------------
# ffr/measurements
# ---------------------------------------------------------------------------

class TestParseFfrMeasurements:
    def test_local_is_house(self):
        payload = [{"channel": "LOCAL", "measurement": "Power", "type": "Active", "value": 11736}]
        readings = parse_ffr_measurements(payload)
        r = next(r for r in readings if r.name == "house_power")
        assert r.value == pytest.approx(11.736)

    def test_house_is_grid(self):
        payload = [{"channel": "HOUSE", "measurement": "Power", "type": "Active", "value": -406464}]
        readings = parse_ffr_measurements(payload)
        r = next(r for r in readings if r.name == "grid_power")
        assert r.value == pytest.approx(-406.464)

    def test_aux1(self):
        payload = [{"channel": "AUX1", "measurement": "Power", "type": "Active", "value": -1480}]
        readings = parse_ffr_measurements(payload)
        r = next(r for r in readings if r.name == "aux_power")
        assert r.value == pytest.approx(-1.48)

    def test_ignores_non_active_power(self):
        payload = [{"channel": "LOCAL", "measurement": "Power", "type": "Apparent", "value": 5000}]
        assert parse_ffr_measurements(payload) == []

    def test_ignores_unknown_channel(self):
        payload = [{"channel": "SOLAR", "measurement": "Power", "type": "Active", "value": 1000}]
        assert parse_ffr_measurements(payload) == []


# ---------------------------------------------------------------------------
# schedule/event
# ---------------------------------------------------------------------------

class TestParseScheduleEvent:
    def test_idle(self):
        readings = parse_schedule_event({"event": 0, "setpoint": 0})
        event_r = next(r for r in readings if r.name == "schedule_event")
        assert event_r.value == pytest.approx(0.0)

    def test_force_charge(self):
        readings = parse_schedule_event(
            {"time": "2025-12-18T07:46:49", "event": 3, "setpoint": 2000}
        )
        event_r = next(r for r in readings if r.name == "schedule_event")
        assert event_r.value == pytest.approx(3.0)
        setpoint_r = next(r for r in readings if r.name == "schedule_setpoint")
        assert setpoint_r.value == pytest.approx(2000.0)
        assert setpoint_r.unit == "W"

    def test_missing_setpoint(self):
        readings = parse_schedule_event({"event": 1})
        assert len(readings) == 1
        assert readings[0].name == "schedule_event"


# ---------------------------------------------------------------------------
# m4/maxpower
# ---------------------------------------------------------------------------

class TestParseMaxpower:
    def test_list_format(self):
        payload = [{"ChgPower": 4792, "DchgPower": -6750}]
        readings = parse_maxpower(payload)
        chg = next(r for r in readings if r.name == "max_charge_power")
        dchg = next(r for r in readings if r.name == "max_discharge_power")
        assert chg.value == pytest.approx(4792.0)
        assert dchg.value == pytest.approx(-6750.0)

    def test_dict_format(self):
        payload = {"ChgPower": 3000, "DchgPower": -5000}
        readings = parse_maxpower(payload)
        assert len(readings) == 2

    def test_empty_list(self):
        assert parse_maxpower([]) == []


# ---------------------------------------------------------------------------
# eps/status
# ---------------------------------------------------------------------------

class TestParseEpsStatus:
    def test_dict_format(self):
        readings = parse_eps_status({"Reserve": 20, "Mode": 0})
        reserve = next(r for r in readings if r.name == "eps_reserve")
        mode = next(r for r in readings if r.name == "eps_mode")
        assert reserve.value == pytest.approx(20.0)
        assert reserve.unit == "%"
        assert mode.value == pytest.approx(0.0)

    def test_list_format(self):
        readings = parse_eps_status([{"Reserve": 15, "Mode": 1}])
        assert len(readings) == 2


# ---------------------------------------------------------------------------
# parse_topic router
# ---------------------------------------------------------------------------

class TestParseTopicRouter:
    def test_known_topic_routed(self):
        payload = [{"measurement": "StateOfCharge", "value": 9700}]
        readings = parse_topic("bms/soc", payload)
        assert len(readings) == 1

    def test_unknown_topic_returns_empty(self):
        assert parse_topic("unknown/topic", {}) == []

    def test_all_registered_topics(self):
        from pv3_monitor.topics import _PARSERS
        # Ensure the router table is not empty
        assert len(_PARSERS) >= 8


# ---------------------------------------------------------------------------
# Schedule event name map
# ---------------------------------------------------------------------------

class TestScheduleEventNames:
    def test_all_codes_named(self):
        for code in range(5):
            assert code in SCHEDULE_EVENT_NAMES

    def test_names(self):
        assert SCHEDULE_EVENT_NAMES[0] == "Idle"
        assert SCHEDULE_EVENT_NAMES[3] == "Force Charge"
        assert SCHEDULE_EVENT_NAMES[4] == "Force Discharge"
