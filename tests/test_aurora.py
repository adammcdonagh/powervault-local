"""Unit tests for abb_aurora.aurora — no real hardware required."""

import pytest
from unittest.mock import MagicMock, patch

from abb_aurora.aurora import AbbAuroraPoller, AuroraSnapshot, _INVERTER_STATES


class TestAuroraSnapshot:
    def test_default_values(self):
        snap = AuroraSnapshot()
        assert snap.ac_power_w == 0.0
        assert snap.dc_power_w == 0.0
        assert snap.is_producing is False
        assert snap.inverter_state is None

    def test_is_producing_threshold(self):
        snap = AuroraSnapshot(ac_power_w=6.0)
        assert snap.is_producing is False  # not set automatically — poller sets it

    def test_inverter_state_map_has_run(self):
        assert _INVERTER_STATES[2] == "Run"

    def test_inverter_state_map_covers_key_codes(self):
        for code in (0, 2, 8, 15, 20):
            assert code in _INVERTER_STATES


class TestAbbAuroraPollerRead:
    def _make_poller_with_mock(self) -> AbbAuroraPoller:
        poller = AbbAuroraPoller(port="/dev/ttyUSBtest", address=2)
        mock_client = MagicMock()
        # Simulate daytime readings
        mock_client.measure.side_effect = lambda dsp: {
            "v_in_1": 320.0,
            "i_in_1": 9.3,
            "v_grid": 235.0,
            "frequency": 50.01,
            "temperature_inverter": 42.5,
            "power_grid": 2800.0,
            "power_in": 3000.0,
        }.get(dsp, None)
        mock_client.cumulated_energy.side_effect = lambda period: {
            "day": 8500,   # Wh → 8.5 kWh
            "total": 12500000,  # Wh → 12500 kWh
        }.get(period, None)
        mock_client.state.return_value = 2  # "Run"
        poller._client = mock_client
        return poller

    def test_ac_power_from_dsp(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.ac_power_w == pytest.approx(2800.0)

    def test_dc_power_from_dsp(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.dc_power_w == pytest.approx(3000.0)

    def test_is_producing_true_when_power_above_threshold(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.is_producing is True

    def test_energy_today_converted_to_kwh(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.energy_today_kwh == pytest.approx(8.5)

    def test_energy_total_converted_to_kwh(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.energy_total_kwh == pytest.approx(12500.0)

    def test_inverter_state_decoded(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.inverter_state == "Run"

    def test_temperature_read(self):
        poller = self._make_poller_with_mock()
        snap = poller.read()
        assert snap.temperature_c == pytest.approx(42.5)

    def test_offline_inverter_returns_zeros_not_exception(self):
        """After sunset the inverter may time out — should return zero values."""
        poller = AbbAuroraPoller(port="/dev/ttyUSBtest", address=2)
        mock_client = MagicMock()
        mock_client.measure.side_effect = Exception("timeout")
        mock_client.cumulated_energy.side_effect = Exception("timeout")
        mock_client.state.side_effect = Exception("timeout")
        poller._client = mock_client

        snap = poller.read()
        assert snap.ac_power_w == 0.0
        assert snap.is_producing is False

    def test_context_manager_calls_close(self):
        poller = AbbAuroraPoller(port="/dev/ttyUSBtest", address=2)
        mock_client = MagicMock()
        poller._client = mock_client

        with poller:
            pass

        mock_client.close.assert_called_once()
        assert poller._client is None

    def test_connect_raises_if_library_missing(self):
        poller = AbbAuroraPoller(port="/dev/ttyUSBtest", address=2)
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            with pytest.raises(ImportError, match="aurorapy"):
                poller._connect()
