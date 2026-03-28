"""Unit tests for pylontech_driver.bms — no real hardware required."""

import pytest
from unittest.mock import MagicMock, patch

from pylontech_driver.bms import (
    BmsPoller,
    BmsSnapshot,
    ModuleData,
    _aggregate,
)


def _make_module(
    address=0,
    voltage_mv=51000,
    current_ma=5000,
    temp_mc=25000,
    soc=80.0,
    soh=98.0,
    cycles=42,
    cell_voltages_mv=None,
    temperatures_mc=None,
):
    """Return a mock pylontech module info object."""
    m = MagicMock()
    m.Voltage = voltage_mv
    m.Current = current_ma
    m.Temperature = temp_mc
    m.RSOC = soc
    m.RSOH = soh
    m.CycleNumber = cycles
    m.CellVoltages = cell_voltages_mv or [3400, 3410, 3420]
    m.Temperatures = temperatures_mc or [25000, 26000]
    return m


def _make_params(
    charge_v=53600,
    discharge_v=44000,
    charge_a=50000,
    discharge_a=50000,
):
    p = MagicMock()
    p.ChargeVoltageLimit = charge_v / 1000.0
    p.DischargeVoltageLimit = discharge_v / 1000.0
    p.ChargeCurrentLimit = charge_a
    p.DischargeCurrentLimit = discharge_a
    return p


class TestAggregate:
    def test_empty_returns_default_snapshot(self):
        snap = _aggregate([])
        assert snap.soc_pct == 0.0
        assert snap.modules == []

    def test_single_module_values(self):
        mod = ModuleData(
            address=0,
            voltage_v=51.0,
            current_a=5.0,
            temperature_c=25.0,
            soc_pct=80.0,
            soh_pct=98.0,
            cycle_count=42,
            cell_voltages_v=[3.4, 3.41, 3.42],
            temperatures_c=[25.0, 26.0],
            charge_voltage_limit_v=53.6,
            discharge_voltage_limit_v=44.0,
            charge_current_limit_a=50.0,
            discharge_current_limit_a=50.0,
        )
        snap = _aggregate([mod])
        assert snap.soc_pct == 80.0
        assert snap.soh_pct == 98.0
        assert snap.battery_voltage_v == 51.0
        assert snap.battery_current_a == 5.0
        assert abs(snap.battery_power_w - 255.0) < 0.01
        assert snap.cell_voltage_max_v == pytest.approx(3.42)
        assert snap.cell_voltage_min_v == pytest.approx(3.40)
        assert snap.cell_temp_avg_c == pytest.approx(25.5)
        assert snap.charge_voltage_limit_v == pytest.approx(53.6)
        assert snap.discharge_current_limit_a == pytest.approx(50.0)

    def test_two_modules_averaged_soc(self):
        mods = [
            ModuleData(address=0, voltage_v=51.0, current_a=3.0, temperature_c=25.0,
                       soc_pct=90.0, soh_pct=98.0, cycle_count=10),
            ModuleData(address=1, voltage_v=51.0, current_a=3.0, temperature_c=25.0,
                       soc_pct=70.0, soh_pct=95.0, cycle_count=20),
        ]
        snap = _aggregate(mods)
        assert snap.soc_pct == 80.0
        assert snap.soh_pct == pytest.approx(96.5)
        assert snap.soh_pct_min == 95.0
        assert snap.battery_current_a == 6.0  # sum
        assert snap.cycle_count_max == 20

    def test_most_restrictive_charge_limit(self):
        mods = [
            ModuleData(address=0, voltage_v=51.0, current_a=0.0, temperature_c=25.0,
                       soc_pct=80.0, soh_pct=98.0, cycle_count=0,
                       charge_current_limit_a=50.0),
            ModuleData(address=1, voltage_v=51.0, current_a=0.0, temperature_c=25.0,
                       soc_pct=80.0, soh_pct=98.0, cycle_count=0,
                       charge_current_limit_a=30.0),  # weaker module
        ]
        snap = _aggregate(mods)
        # Should use the lower (more restrictive) limit
        assert snap.charge_current_limit_a == 30.0

    def test_most_restrictive_discharge_limit(self):
        mods = [
            ModuleData(address=0, voltage_v=51.0, current_a=0.0, temperature_c=25.0,
                       soc_pct=80.0, soh_pct=98.0, cycle_count=0,
                       discharge_current_limit_a=50.0),
            ModuleData(address=1, voltage_v=51.0, current_a=0.0, temperature_c=25.0,
                       soc_pct=80.0, soh_pct=98.0, cycle_count=0,
                       discharge_current_limit_a=40.0),
        ]
        snap = _aggregate(mods)
        assert snap.discharge_current_limit_a == 40.0

    def test_missing_limits_are_none(self):
        mod = ModuleData(address=0, voltage_v=51.0, current_a=0.0, temperature_c=25.0,
                         soc_pct=80.0, soh_pct=98.0, cycle_count=0)
        snap = _aggregate([mod])
        assert snap.charge_current_limit_a is None
        assert snap.discharge_current_limit_a is None


class TestBmsPollerRead:
    def _make_poller_with_mock(self, mock_info, mock_params, num_modules=1):
        """Return a BmsPoller whose internal _pylontech is fully mocked."""
        poller = BmsPoller(port="/dev/ttyUSBtest", num_modules=num_modules)
        mock_pylon = MagicMock()
        mock_pylon.get_values_single.return_value = mock_info
        mock_pylon.get_system_parameters.return_value = mock_params
        poller._pylontech = mock_pylon
        return poller

    def test_single_module_read(self):
        info = _make_module()
        params = _make_params()
        poller = self._make_poller_with_mock(info, params)

        snap = poller.read()

        assert isinstance(snap, BmsSnapshot)
        assert snap.soc_pct == 80.0
        assert snap.battery_voltage_v == pytest.approx(51.0)
        assert snap.battery_current_a == pytest.approx(5.0)
        assert len(snap.modules) == 1

    def test_charge_current_limit_converted_from_ma(self):
        info = _make_module()
        params = _make_params(charge_a=50000)  # 50000 mA
        poller = self._make_poller_with_mock(info, params)

        snap = poller.read()
        # 50000 mA → 50.0 A
        assert snap.charge_current_limit_a == pytest.approx(50.0)

    def test_failed_module_excluded_from_aggregate(self):
        """If one module fails communication the others are still returned."""
        poller = BmsPoller(port="/dev/ttyUSBtest", num_modules=2)
        mock_pylon = MagicMock()
        # Module 0 succeeds, module 1 raises
        mock_pylon.get_values_single.side_effect = [
            _make_module(address=0),
            Exception("timeout"),
        ]
        mock_pylon.get_system_parameters.return_value = _make_params()
        poller._pylontech = mock_pylon

        snap = poller.read()
        assert len(snap.modules) == 1
        assert snap.modules[0].address == 0

    def test_all_modules_fail_raises_runtime_error(self):
        poller = BmsPoller(port="/dev/ttyUSBtest", num_modules=1)
        mock_pylon = MagicMock()
        mock_pylon.get_values_single.side_effect = Exception("no response")
        poller._pylontech = mock_pylon

        with pytest.raises(RuntimeError, match="No battery modules"):
            poller.read()

    def test_connect_raises_if_library_missing(self):
        poller = BmsPoller(port="/dev/ttyUSBtest")
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            with pytest.raises(ImportError, match="python-pylontech"):
                poller._connect()

    def test_context_manager_calls_close(self):
        poller = BmsPoller(port="/dev/ttyUSBtest")
        mock_pylon = MagicMock()
        poller._pylontech = mock_pylon

        with poller:
            pass

        mock_pylon.close.assert_called_once()
        assert poller._pylontech is None
