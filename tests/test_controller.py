"""Unit tests for controller.safety and controller.ha_discovery."""

import json
import pytest
from unittest.mock import MagicMock, patch

from pylontech_driver.bms import BmsSnapshot, ModuleData
from controller.safety import (
    SafetyError,
    apply_bms_limits,
    check_charge_allowed,
    check_discharge_allowed,
    MAX_CHARGE_SOC_PCT,
    MIN_DISCHARGE_SOC_PCT,
)
from controller import ha_discovery
from controller.ha_discovery import CHARGE_MODES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bms(
    soc=80.0,
    soh=98.0,
    voltage=51.0,
    current=0.0,
    charge_current_limit=50.0,
    discharge_current_limit=50.0,
    charge_voltage_limit=53.6,
    discharge_voltage_limit=44.0,
) -> BmsSnapshot:
    mod = ModuleData(
        address=0,
        voltage_v=voltage,
        current_a=current,
        temperature_c=25.0,
        soc_pct=soc,
        soh_pct=soh,
        cycle_count=42,
        charge_current_limit_a=charge_current_limit,
        discharge_current_limit_a=discharge_current_limit,
        charge_voltage_limit_v=charge_voltage_limit,
        discharge_voltage_limit_v=discharge_voltage_limit,
    )
    from pylontech_driver.bms import _aggregate
    return _aggregate([mod])


# ---------------------------------------------------------------------------
# Safety tests
# ---------------------------------------------------------------------------

class TestApplyBmsLimits:
    def test_sets_charge_and_discharge_current(self):
        bms = _bms(charge_current_limit=40.0, discharge_current_limit=35.0)
        inv = MagicMock()
        apply_bms_limits(inv, bms)
        inv.set_max_charge_current.assert_called_once_with(40)
        inv.set_ac_charge_current.assert_called_once_with(40)

    def test_clamps_to_hardware_max(self):
        bms = _bms(charge_current_limit=150.0)  # exceeds hardware max
        inv = MagicMock()
        apply_bms_limits(inv, bms)
        inv.set_max_charge_current.assert_called_once_with(100)  # clamped

    def test_sets_voltage_limits(self):
        bms = _bms(charge_voltage_limit=53.6, discharge_voltage_limit=44.0)
        inv = MagicMock()
        apply_bms_limits(inv, bms)
        inv.set_battery_recharge_voltage.assert_called_once_with(pytest.approx(53.6))
        inv.set_battery_cutoff_voltage.assert_called_once_with(pytest.approx(44.0))

    def test_raises_safety_error_when_limits_missing(self):
        bms = _bms()
        bms.charge_current_limit_a = None  # simulate BMS unreachable
        inv = MagicMock()
        with pytest.raises(SafetyError, match="unavailable"):
            apply_bms_limits(inv, bms)


class TestCheckChargeAllowed:
    def test_allowed_when_below_ceiling(self):
        bms = _bms(soc=MAX_CHARGE_SOC_PCT - 1)
        check_charge_allowed(bms)  # should not raise

    def test_blocked_at_ceiling(self):
        bms = _bms(soc=MAX_CHARGE_SOC_PCT)
        with pytest.raises(SafetyError, match="charge not needed"):
            check_charge_allowed(bms)

    def test_blocked_above_ceiling(self):
        bms = _bms(soc=MAX_CHARGE_SOC_PCT + 5)
        with pytest.raises(SafetyError):
            check_charge_allowed(bms)


class TestCheckDischargeAllowed:
    def test_allowed_when_above_floor(self):
        bms = _bms(soc=MIN_DISCHARGE_SOC_PCT + 1)
        check_discharge_allowed(bms)  # should not raise

    def test_blocked_at_floor(self):
        bms = _bms(soc=MIN_DISCHARGE_SOC_PCT)
        with pytest.raises(SafetyError, match="discharge not permitted"):
            check_discharge_allowed(bms)

    def test_blocked_below_floor(self):
        bms = _bms(soc=MIN_DISCHARGE_SOC_PCT - 1)
        with pytest.raises(SafetyError):
            check_discharge_allowed(bms)


# ---------------------------------------------------------------------------
# HA discovery tests
# ---------------------------------------------------------------------------

DEVICE_ID = "testdev"


class TestAllDiscoveryConfigs:
    def test_returns_non_empty_list(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        assert isinstance(configs, list)
        assert len(configs) > 0

    def test_all_payloads_are_valid_json(self):
        for _, payload_str in ha_discovery.all_discovery_configs(DEVICE_ID):
            payload = json.loads(payload_str)
            assert isinstance(payload, dict)

    def test_all_payloads_have_required_ha_fields(self):
        for _, payload_str in ha_discovery.all_discovery_configs(DEVICE_ID):
            payload = json.loads(payload_str)
            assert "name" in payload
            assert "unique_id" in payload
            assert "state_topic" in payload
            assert "device" in payload
            assert "availability_topic" in payload

    def test_all_availability_topics_match_device(self):
        expected = ha_discovery.availability_topic(DEVICE_ID)
        for _, payload_str in ha_discovery.all_discovery_configs(DEVICE_ID):
            payload = json.loads(payload_str)
            assert payload["availability_topic"] == expected

    def test_unique_ids_are_globally_unique(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        unique_ids = [json.loads(p)["unique_id"] for _, p in configs]
        assert len(unique_ids) == len(set(unique_ids))

    def test_discovery_topics_are_globally_unique(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        topics = [t for t, _ in configs]
        assert len(topics) == len(set(topics))

    def test_charge_mode_select_is_included(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        select_topics = [t for t, _ in configs if t.startswith("homeassistant/select/")]
        assert len(select_topics) == 1

    def test_bms_soc_sensor_included(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        topics = [t for t, _ in configs]
        assert any("battery_soc" in t for t in topics)

    def test_pv_power_sensor_included(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        topics = [t for t, _ in configs]
        assert any("pv_ac_power" in t for t in topics)

    def test_binary_sensor_for_pv_producing(self):
        configs = ha_discovery.all_discovery_configs(DEVICE_ID)
        binary_topics = [t for t, _ in configs if t.startswith("homeassistant/binary_sensor/")]
        assert len(binary_topics) >= 1
        assert any("pv_producing" in t for t in binary_topics)

    def test_device_id_scoped_unique_ids(self):
        """Unique IDs from two different device IDs must not clash."""
        ids_a = {json.loads(p)["unique_id"] for _, p in ha_discovery.all_discovery_configs("devA")}
        ids_b = {json.loads(p)["unique_id"] for _, p in ha_discovery.all_discovery_configs("devB")}
        assert ids_a.isdisjoint(ids_b)


class TestChargeModeSelectDiscovery:
    def test_returns_select_component_topic(self):
        topic, _ = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        assert topic.startswith("homeassistant/select/")

    def test_all_charge_modes_present(self):
        _, payload_str = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        payload = json.loads(payload_str)
        assert set(payload["options"]) == set(CHARGE_MODES)

    def test_command_topic_correct(self):
        _, payload_str = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        payload = json.loads(payload_str)
        assert payload["command_topic"] == ha_discovery.charge_mode_command_topic(DEVICE_ID)

    def test_state_topic_correct(self):
        _, payload_str = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        payload = json.loads(payload_str)
        assert payload["state_topic"] == ha_discovery.charge_mode_state_topic(DEVICE_ID)

    def test_command_and_state_topics_differ(self):
        _, payload_str = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        payload = json.loads(payload_str)
        assert payload["command_topic"] != payload["state_topic"]

    def test_payload_has_device_block(self):
        _, payload_str = ha_discovery.charge_mode_select_discovery(DEVICE_ID)
        payload = json.loads(payload_str)
        assert "device" in payload
        assert "identifiers" in payload["device"]


class TestChargeModes:
    def test_five_modes(self):
        assert len(CHARGE_MODES) == 5

    def test_idle_present(self):
        assert "idle" in CHARGE_MODES

    def test_force_modes_present(self):
        assert "force_charge" in CHARGE_MODES
        assert "force_discharge" in CHARGE_MODES

    def test_all_modes_lowercase(self):
        for mode in CHARGE_MODES:
            assert mode == mode.lower()


# ---------------------------------------------------------------------------
# _parse_shelly_power tests
# ---------------------------------------------------------------------------

from controller.main import _parse_shelly_power, _effective_mode


class TestParseShellPower:
    def test_plain_float_string(self):
        assert _parse_shelly_power(b"1234.5") == pytest.approx(1234.5)

    def test_plain_negative_float(self):
        assert _parse_shelly_power(b"-400.0") == pytest.approx(-400.0)

    def test_plain_integer_string(self):
        assert _parse_shelly_power(b"2000") == pytest.approx(2000.0)

    def test_gen1_with_whitespace(self):
        assert _parse_shelly_power(b"  850.3\n") == pytest.approx(850.3)

    def test_gen2_json_apower(self):
        payload = json.dumps({"id": 0, "apower": 1500.0, "voltage": 230.0}).encode()
        assert _parse_shelly_power(payload) == pytest.approx(1500.0)

    def test_gen2_json_act_power(self):
        payload = json.dumps({"act_power": 750.5}).encode()
        assert _parse_shelly_power(payload) == pytest.approx(750.5)

    def test_gen2_json_power_key(self):
        payload = json.dumps({"power": 300.0}).encode()
        assert _parse_shelly_power(payload) == pytest.approx(300.0)

    def test_gen3_json_a_act_power(self):
        payload = json.dumps({"a_act_power": 1800.0}).encode()
        assert _parse_shelly_power(payload) == pytest.approx(1800.0)

    def test_json_null_power_falls_through(self):
        # value is null — should not match, return None
        payload = json.dumps({"power": None}).encode()
        assert _parse_shelly_power(payload) is None

    def test_invalid_payload_returns_none(self):
        assert _parse_shelly_power(b"not-a-number") is None

    def test_empty_payload_returns_none(self):
        assert _parse_shelly_power(b"") is None

    def test_json_no_known_key_returns_none(self):
        payload = json.dumps({"reactive": 50.0}).encode()
        assert _parse_shelly_power(payload) is None


# ---------------------------------------------------------------------------
# _effective_mode tests
# ---------------------------------------------------------------------------

class TestEffectiveMode:
    def _call(
        self,
        requested="idle",
        grid_w=None,
        peak_shave=False,
        threshold=0.0,
        hysteresis=50.0,
    ) -> str:
        return _effective_mode(requested, grid_w, peak_shave, threshold, hysteresis)

    def test_returns_requested_when_peak_shave_disabled(self):
        assert self._call("discharge", grid_w=5000.0, peak_shave=False) == "discharge"

    def test_returns_requested_when_no_grid_reading(self):
        assert self._call("idle", grid_w=None, peak_shave=True, threshold=0.0) == "idle"

    def test_override_to_discharge_above_threshold_plus_hysteresis(self):
        # threshold=0, hysteresis=50 → triggers above 50 W
        assert self._call("idle", grid_w=51.0, peak_shave=True, threshold=0.0, hysteresis=50.0) == "discharge"

    def test_no_override_exactly_at_threshold_plus_hysteresis(self):
        # must be strictly greater than threshold + hysteresis
        assert self._call("idle", grid_w=50.0, peak_shave=True, threshold=0.0, hysteresis=50.0) == "idle"

    def test_no_override_below_threshold(self):
        assert self._call("idle", grid_w=30.0, peak_shave=True, threshold=0.0, hysteresis=50.0) == "idle"

    def test_no_override_when_exporting(self):
        assert self._call("idle", grid_w=-200.0, peak_shave=True, threshold=0.0) == "idle"

    def test_existing_mode_preserved_below_threshold(self):
        # HA has requested "charge"; grid is low — honour the request
        assert self._call("charge", grid_w=10.0, peak_shave=True, threshold=100.0, hysteresis=50.0) == "charge"

    def test_override_even_when_requested_mode_is_charge(self):
        # Spike while HA requested charge — safety: prioritise peak-shaving
        assert self._call("charge", grid_w=2000.0, peak_shave=True, threshold=500.0, hysteresis=50.0) == "discharge"

    def test_custom_threshold(self):
        # threshold=3000, hysteresis=50 → triggers above 3050 W
        assert self._call("idle", grid_w=3100.0, peak_shave=True, threshold=3000.0, hysteresis=50.0) == "discharge"
        assert self._call("idle", grid_w=3000.0, peak_shave=True, threshold=3000.0, hysteresis=50.0) == "idle"


class TestTopicHelpers:
    def test_sensor_state_topic_contains_device_and_name(self):
        topic = ha_discovery.sensor_state_topic(DEVICE_ID, "battery_soc")
        assert DEVICE_ID in topic
        assert "battery_soc" in topic

    def test_charge_mode_command_topic_contains_device(self):
        topic = ha_discovery.charge_mode_command_topic(DEVICE_ID)
        assert DEVICE_ID in topic

    def test_charge_mode_state_topic_contains_device(self):
        topic = ha_discovery.charge_mode_state_topic(DEVICE_ID)
        assert DEVICE_ID in topic

    def test_command_and_state_topics_differ(self):
        cmd = ha_discovery.charge_mode_command_topic(DEVICE_ID)
        state = ha_discovery.charge_mode_state_topic(DEVICE_ID)
        assert cmd != state

    def test_availability_topic_contains_device(self):
        topic = ha_discovery.availability_topic(DEVICE_ID)
        assert DEVICE_ID in topic

    def test_availability_differs_from_sensor_topics(self):
        avail = ha_discovery.availability_topic(DEVICE_ID)
        sensor = ha_discovery.sensor_state_topic(DEVICE_ID, "battery_soc")
        assert avail != sensor

