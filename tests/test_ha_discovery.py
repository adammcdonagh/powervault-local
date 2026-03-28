"""
Tests for pv3_monitor.ha_discovery — Home Assistant MQTT auto-discovery payloads.
"""

import json
import pytest
from pv3_monitor.ha_discovery import (
    SCHEDULE_OPTIONS,
    control_topic_for,
    make_device_info,
    schedule_select_config,
    sensor_config,
    setpoint_number_config,
    state_topic_for,
)
from pv3_monitor.topics import SensorReading


DEVICE_ID = "PV001001DEV"


class TestMakeDeviceInfo:
    def test_contains_required_keys(self):
        info = make_device_info(DEVICE_ID)
        assert "identifiers" in info
        assert "name" in info
        assert "manufacturer" in info

    def test_identifier_contains_device_id(self):
        info = make_device_info(DEVICE_ID)
        assert any(DEVICE_ID in ident for ident in info["identifiers"])


class TestSensorConfig:
    def _make_reading(self, name="soc", unit="%", device_class="battery"):
        return SensorReading(
            name=name,
            value=97.0,
            unit=unit,
            device_class=device_class,
            friendly_name="Battery SoC",
        )

    def test_returns_two_strings(self):
        r = self._make_reading()
        topic, payload_str = sensor_config(r, DEVICE_ID, "some/topic")
        assert isinstance(topic, str)
        assert isinstance(payload_str, str)

    def test_payload_is_valid_json(self):
        r = self._make_reading()
        _, payload_str = sensor_config(r, DEVICE_ID, "some/topic")
        payload = json.loads(payload_str)
        assert isinstance(payload, dict)

    def test_required_ha_fields(self):
        r = self._make_reading()
        _, payload_str = sensor_config(r, DEVICE_ID, "some/state/topic")
        payload = json.loads(payload_str)
        assert payload["unique_id"].startswith("pv3_")
        assert payload["state_topic"] == "some/state/topic"
        assert "device" in payload

    def test_unit_included_when_set(self):
        r = self._make_reading(unit="V")
        _, payload_str = sensor_config(r, DEVICE_ID, "t")
        payload = json.loads(payload_str)
        assert payload["unit_of_measurement"] == "V"

    def test_unit_omitted_when_empty(self):
        r = SensorReading(name="alarm_count", value=0.0, unit="")
        _, payload_str = sensor_config(r, DEVICE_ID, "t")
        payload = json.loads(payload_str)
        assert "unit_of_measurement" not in payload

    def test_device_class_included(self):
        r = self._make_reading(device_class="battery")
        _, payload_str = sensor_config(r, DEVICE_ID, "t")
        payload = json.loads(payload_str)
        assert payload["device_class"] == "battery"

    def test_discovery_topic_format(self):
        r = self._make_reading(name="soc")
        topic, _ = sensor_config(r, DEVICE_ID, "t")
        assert topic.startswith("homeassistant/sensor/")
        assert DEVICE_ID in topic

    def test_unique_id_scoped_to_device(self):
        r = self._make_reading(name="soc")
        _, payload_str = sensor_config(r, DEVICE_ID, "t")
        payload = json.loads(payload_str)
        assert DEVICE_ID in payload["unique_id"]


class TestScheduleSelectConfig:
    def test_returns_two_strings(self):
        topic, payload_str = schedule_select_config(DEVICE_ID, "cmd/topic", "state/topic")
        assert isinstance(topic, str)
        assert isinstance(payload_str, str)

    def test_payload_has_options(self):
        _, payload_str = schedule_select_config(DEVICE_ID, "cmd", "state")
        payload = json.loads(payload_str)
        assert "options" in payload
        assert set(payload["options"]) == set(SCHEDULE_OPTIONS)

    def test_command_and_state_topics_set(self):
        _, payload_str = schedule_select_config(DEVICE_ID, "my/cmd", "my/state")
        payload = json.loads(payload_str)
        assert payload["command_topic"] == "my/cmd"
        assert payload["state_topic"] == "my/state"

    def test_discovery_topic_is_select_type(self):
        topic, _ = schedule_select_config(DEVICE_ID, "c", "s")
        assert topic.startswith("homeassistant/select/")

    def test_device_present(self):
        _, payload_str = schedule_select_config(DEVICE_ID, "c", "s")
        payload = json.loads(payload_str)
        assert "device" in payload


class TestSetpointNumberConfig:
    def test_returns_two_strings(self):
        topic, payload_str = setpoint_number_config(DEVICE_ID, "cmd", "state")
        assert isinstance(topic, str)
        assert isinstance(payload_str, str)

    def test_payload_has_range(self):
        _, payload_str = setpoint_number_config(DEVICE_ID, "cmd", "state")
        payload = json.loads(payload_str)
        assert payload["min"] >= 0
        assert payload["max"] > payload["min"]

    def test_discovery_topic_is_number_type(self):
        topic, _ = setpoint_number_config(DEVICE_ID, "c", "s")
        assert topic.startswith("homeassistant/number/")

    def test_unit_is_watts(self):
        _, payload_str = setpoint_number_config(DEVICE_ID, "c", "s")
        payload = json.loads(payload_str)
        assert payload["unit_of_measurement"] == "W"


class TestTopicHelpers:
    def test_state_topic_format(self):
        topic = state_topic_for(DEVICE_ID, "soc")
        assert DEVICE_ID in topic
        assert "soc" in topic
        assert topic.startswith("powervault/")

    def test_control_topic_format(self):
        topic = control_topic_for(DEVICE_ID, "schedule/mode")
        assert DEVICE_ID in topic
        assert "schedule" in topic
        assert topic.startswith("powervault/")

    def test_state_and_control_differ(self):
        s = state_topic_for(DEVICE_ID, "x")
        c = control_topic_for(DEVICE_ID, "x")
        assert s != c


class TestScheduleOptions:
    def test_five_options(self):
        assert len(SCHEDULE_OPTIONS) == 5

    def test_contains_force_modes(self):
        assert "Force Charge" in SCHEDULE_OPTIONS
        assert "Force Discharge" in SCHEDULE_OPTIONS

    def test_contains_idle(self):
        assert "Idle" in SCHEDULE_OPTIONS
