"""Unit tests for controller.safety and controller.scheduler."""

import pytest
from datetime import datetime, time
from unittest.mock import MagicMock, patch, call

from pylontech_driver.bms import BmsSnapshot, ModuleData
from abb_aurora.aurora import AuroraSnapshot
from controller.safety import (
    SafetyError,
    apply_bms_limits,
    check_charge_allowed,
    check_discharge_allowed,
    MAX_CHARGE_SOC_PCT,
    MIN_DISCHARGE_SOC_PCT,
)
from controller.scheduler import decide_mode, _in_cheap_window


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


def _pv(ac_power=0.0, producing=False) -> AuroraSnapshot:
    snap = AuroraSnapshot(ac_power_w=ac_power)
    snap.is_producing = producing
    return snap


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
# Scheduler tests
# ---------------------------------------------------------------------------

class TestInCheapWindow:
    def test_in_window(self):
        assert _in_cheap_window(time(1, 0), time(0, 30), time(4, 30))

    def test_at_window_start(self):
        assert _in_cheap_window(time(0, 30), time(0, 30), time(4, 30))

    def test_at_window_end_is_exclusive(self):
        assert not _in_cheap_window(time(4, 30), time(0, 30), time(4, 30))

    def test_outside_window(self):
        assert not _in_cheap_window(time(10, 0), time(0, 30), time(4, 30))

    def test_midnight_wrap(self):
        # 23:30 → 04:30 wrapping window
        assert _in_cheap_window(time(23, 45), time(23, 30), time(4, 30))
        assert _in_cheap_window(time(0, 0), time(23, 30), time(4, 30))
        assert not _in_cheap_window(time(10, 0), time(23, 30), time(4, 30))


class TestDecideMode:
    # --- Safety floor ---
    def test_force_charge_at_soc_floor(self):
        bms = _bms(soc=15.0)  # == MIN_DISCHARGE_SOC_PCT
        mode = decide_mode(bms, _pv(), 500.0, now=datetime(2024, 1, 15, 14, 0))
        assert mode == "force_charge"

    def test_force_charge_below_soc_floor(self):
        bms = _bms(soc=10.0)
        mode = decide_mode(bms, _pv(), 500.0, now=datetime(2024, 1, 15, 14, 0))
        assert mode == "force_charge"

    # --- Cheap tariff window ---
    def test_charges_during_cheap_when_below_target(self):
        bms = _bms(soc=60.0)  # below 90% target
        mode = decide_mode(
            bms, _pv(), 500.0,
            now=datetime(2024, 1, 15, 2, 0),  # 02:00, inside cheap window
        )
        assert mode == "charge"

    def test_idle_during_cheap_when_at_target(self):
        bms = _bms(soc=91.0)  # above 90% target
        mode = decide_mode(
            bms, _pv(), 500.0,
            now=datetime(2024, 1, 15, 2, 0),
        )
        assert mode == "idle"

    # --- Daytime, solar available ---
    def test_charges_from_pv_when_excess_solar_and_battery_not_full(self):
        bms = _bms(soc=70.0)
        pv = _pv(ac_power=3000.0, producing=True)
        mode = decide_mode(
            bms, pv, house_power_w=1500.0,
            now=datetime(2024, 6, 15, 12, 0),  # midday
        )
        assert mode == "charge"

    def test_idle_when_excess_solar_and_battery_full(self):
        bms = _bms(soc=92.0)  # above target
        pv = _pv(ac_power=3000.0, producing=True)
        mode = decide_mode(
            bms, pv, house_power_w=1500.0,
            now=datetime(2024, 6, 15, 12, 0),
        )
        assert mode == "idle"

    def test_discharges_when_solar_less_than_house_demand(self):
        bms = _bms(soc=80.0)
        pv = _pv(ac_power=800.0, producing=True)
        mode = decide_mode(
            bms, pv, house_power_w=2000.0,
            now=datetime(2024, 6, 15, 12, 0),
        )
        assert mode == "discharge"

    # --- Night, no PV ---
    def test_discharges_at_night_with_soc_above_floor(self):
        bms = _bms(soc=60.0)
        mode = decide_mode(
            bms, _pv(producing=False), house_power_w=500.0,
            now=datetime(2024, 1, 15, 22, 0),  # 22:00, outside cheap window
        )
        assert mode == "discharge"

    def test_force_charge_at_night_at_soc_floor(self):
        bms = _bms(soc=MIN_DISCHARGE_SOC_PCT)
        # The floor check runs first so this returns force_charge
        mode = decide_mode(
            bms, _pv(producing=False), house_power_w=500.0,
            now=datetime(2024, 1, 15, 22, 0),
        )
        # At exactly the floor the safety guard fires first
        assert mode == "force_charge"

    def test_uses_env_var_cheap_start(self):
        """CHEAP_START env var changes the cheap window."""
        bms = _bms(soc=50.0)
        with patch.dict("os.environ", {"CHEAP_START": "01:00", "CHEAP_END": "05:00"}):
            mode = decide_mode(
                bms, _pv(), 500.0,
                now=datetime(2024, 1, 15, 0, 30),  # 00:30 — before new window
            )
        # 00:30 is outside 01:00–05:00 so it should fall through to night discharge
        assert mode == "discharge"
