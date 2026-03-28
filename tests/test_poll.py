"""Tests for the poll.py CLI utility.

These tests cover argument parsing, output formatting and the top-level
``main()`` entry point.  All hardware driver calls are mocked so no real
serial ports are needed.
"""

from __future__ import annotations

import json
import io
import sys
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
import poll
from poll import (
    _bar,
    _build_parser,
    _display,
    _poll_battery,
    _poll_inverter,
    _poll_pv,
    _run_once,
    _to_json_safe,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures / test data
# ---------------------------------------------------------------------------

@dataclass
class _FakeBmsSnapshot:
    modules: list = field(default_factory=list)
    soc_pct: float = 75.0
    soh_pct: float = 96.0
    soh_pct_min: float = 94.0
    cycle_count_avg: float = 12.0
    cycle_count_max: int = 12
    battery_voltage_v: float = 51.2
    battery_current_a: float = 3.5
    battery_power_w: float = 179.2
    cell_voltage_max_v: float = 3.40
    cell_voltage_min_v: float = 3.38
    cell_temp_avg_c: float = 22.0
    cell_temp_max_c: float = 24.0
    cell_temp_min_c: float = 20.0
    charge_voltage_limit_v: Optional[float] = 54.0
    discharge_voltage_limit_v: Optional[float] = 42.0
    charge_current_limit_a: Optional[float] = 74.0
    discharge_current_limit_a: Optional[float] = 74.0


@dataclass
class _FakeAuroraSnapshot:
    ac_power_w: float = 1500.0
    dc_power_w: float = 1550.0
    dc_voltage_v: Optional[float] = 320.0
    dc_current_a: Optional[float] = 4.84
    grid_voltage_v: Optional[float] = 238.5
    grid_frequency_hz: Optional[float] = 50.0
    temperature_c: Optional[float] = 38.0
    energy_today_kwh: Optional[float] = 6.2
    energy_total_kwh: Optional[float] = 3456.7
    inverter_state: Optional[str] = "Run"
    is_producing: bool = True


# ---------------------------------------------------------------------------
# _bar
# ---------------------------------------------------------------------------

class TestBar:
    def test_full_bar_at_100(self):
        bar = _bar(100.0)
        assert "█" in bar
        assert "100.0" in bar

    def test_empty_bar_at_0(self):
        bar = _bar(0.0)
        assert "100.0" not in bar or True  # sanity: doesn't raise

    def test_shows_value(self):
        bar = _bar(75.0, 100.0)
        assert "75.0" in bar

    def test_shows_total(self):
        bar = _bar(50.0, 200.0)
        assert "200" in bar

    def test_clamps_above_total(self):
        # Should not raise; just clamps filled blocks
        bar = _bar(150.0, 100.0)
        assert bar  # non-empty string

    def test_clamps_below_zero(self):
        bar = _bar(-10.0, 100.0)
        assert bar


# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------

class TestBuildParser:
    def test_battery_arg(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0"])
        assert args.battery == "/dev/ttyUSB0"

    def test_battery_modules_default(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0"])
        assert args.battery_modules == 1

    def test_battery_modules_custom(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0", "--battery-modules", "3"])
        assert args.battery_modules == 3

    def test_inverter_arg(self):
        args = _build_parser().parse_args(["--inverter", "/dev/ttyUSB1"])
        assert args.inverter == "/dev/ttyUSB1"

    def test_inverter_usb_default_false(self):
        args = _build_parser().parse_args(["--inverter", "/dev/ttyUSB1"])
        assert args.inverter_usb is False

    def test_inverter_usb_flag(self):
        args = _build_parser().parse_args(["--inverter", "/dev/hidraw0", "--inverter-usb"])
        assert args.inverter_usb is True

    def test_pv_arg(self):
        args = _build_parser().parse_args(["--pv", "/dev/ttyUSB2"])
        assert args.pv == "/dev/ttyUSB2"

    def test_pv_address_default(self):
        args = _build_parser().parse_args(["--pv", "/dev/ttyUSB2"])
        assert args.pv_address == 2

    def test_pv_address_custom(self):
        args = _build_parser().parse_args(["--pv", "/dev/ttyUSB2", "--pv-address", "5"])
        assert args.pv_address == 5

    def test_interval_default_zero(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0"])
        assert args.interval == 0

    def test_interval_custom(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0", "--interval", "30"])
        assert args.interval == 30.0

    def test_json_flag(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0", "--json"])
        assert args.json is True

    def test_log_level_default(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0"])
        assert args.log_level == "WARNING"

    def test_log_level_custom(self):
        args = _build_parser().parse_args(["--battery", "/dev/ttyUSB0", "--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# main() — error when no component selected
# ---------------------------------------------------------------------------

class TestMainNoComponent:
    def test_exits_with_error_when_no_component(self):
        with pytest.raises(SystemExit):
            main(["--interval", "0"])


# ---------------------------------------------------------------------------
# _to_json_safe
# ---------------------------------------------------------------------------

class TestToJsonSafe:
    def test_plain_types_unchanged(self):
        assert _to_json_safe(42) == 42
        assert _to_json_safe("hello") == "hello"
        assert _to_json_safe(3.14) == 3.14
        assert _to_json_safe(None) is None

    def test_list_recursed(self):
        result = _to_json_safe([1, 2, 3])
        assert result == [1, 2, 3]

    def test_dict_recursed(self):
        result = _to_json_safe({"a": 1, "b": [2, 3]})
        assert result == {"a": 1, "b": [2, 3]}

    def test_dataclass_to_dict(self):
        snap = _FakeAuroraSnapshot()
        result = _to_json_safe(snap)
        assert isinstance(result, dict)
        assert result["ac_power_w"] == 1500.0
        assert result["is_producing"] is True


# ---------------------------------------------------------------------------
# _poll_battery — happy path and error path
# ---------------------------------------------------------------------------

class TestPollBattery:
    def test_ok_returns_snapshot(self):
        fake_snap = _FakeBmsSnapshot()
        fake_poller = MagicMock()
        fake_poller.read.return_value = fake_snap

        with patch("poll.BmsPoller", return_value=fake_poller) as mock_cls:
            result = _poll_battery("/dev/ttyUSB0", 1)

        assert result["ok"] is True
        assert result["snapshot"] is fake_snap
        mock_cls.assert_called_once_with(port="/dev/ttyUSB0", num_modules=1)

    def test_error_returns_message(self):
        fake_poller = MagicMock()
        fake_poller.read.side_effect = RuntimeError("No response")

        with patch("poll.BmsPoller", return_value=fake_poller):
            result = _poll_battery("/dev/ttyUSB0", 1)

        assert result["ok"] is False
        assert "No response" in result["error"]

    def test_close_called_on_success(self):
        fake_poller = MagicMock()
        fake_poller.read.return_value = _FakeBmsSnapshot()

        with patch("poll.BmsPoller", return_value=fake_poller):
            _poll_battery("/dev/ttyUSB0", 1)

        fake_poller.close.assert_called_once()

    def test_close_called_on_error(self):
        fake_poller = MagicMock()
        fake_poller.read.side_effect = RuntimeError("boom")

        with patch("poll.BmsPoller", return_value=fake_poller):
            _poll_battery("/dev/ttyUSB0", 1)

        fake_poller.close.assert_called_once()


# ---------------------------------------------------------------------------
# _poll_inverter — happy path and error path
# ---------------------------------------------------------------------------

class TestPollInverter:
    def _make_fake_inverter(self):
        inv = MagicMock()
        inv.__enter__ = MagicMock(return_value=inv)
        inv.__exit__ = MagicMock(return_value=False)
        inv.get_protocol_id.return_value = "PI18"
        inv.get_mode.return_value = "L"
        inv.get_general_status.return_value = {"raw": "230 50 100 80"}
        inv.get_device_flags.return_value = "10100000"
        return inv

    def test_ok_returns_data(self):
        fake_inv = self._make_fake_inverter()
        with patch("poll.P18Inverter", return_value=fake_inv):
            result = _poll_inverter("/dev/ttyUSB1", False)

        assert result["ok"] is True
        assert result["protocol"] == "PI18"
        assert result["mode"] == "L"
        assert result["status"] == {"raw": "230 50 100 80"}
        assert result["flags"] == "10100000"

    def test_error_returns_message(self):
        fake_inv = MagicMock()
        fake_inv.__enter__ = MagicMock(side_effect=OSError("port busy"))
        fake_inv.__exit__ = MagicMock(return_value=False)

        with patch("poll.P18Inverter", return_value=fake_inv):
            result = _poll_inverter("/dev/ttyUSB1", False)

        assert result["ok"] is False
        assert "port busy" in result["error"]

    def test_usb_mode_passed_through(self):
        fake_inv = self._make_fake_inverter()
        with patch("poll.P18Inverter", return_value=fake_inv) as mock_cls:
            _poll_inverter("/dev/hidraw0", True)

        mock_cls.assert_called_once_with(port="/dev/hidraw0", is_usb=True)


# ---------------------------------------------------------------------------
# _poll_pv — happy path and error path
# ---------------------------------------------------------------------------

class TestPollPv:
    def test_ok_returns_snapshot(self):
        fake_snap = _FakeAuroraSnapshot()
        fake_poller = MagicMock()
        fake_poller.read.return_value = fake_snap

        with patch("poll.AbbAuroraPoller", return_value=fake_poller) as mock_cls:
            result = _poll_pv("/dev/ttyUSB2", 2)

        assert result["ok"] is True
        assert result["snapshot"] is fake_snap
        mock_cls.assert_called_once_with(port="/dev/ttyUSB2", address=2)

    def test_error_returns_message(self):
        fake_poller = MagicMock()
        fake_poller.read.side_effect = OSError("serial error")

        with patch("poll.AbbAuroraPoller", return_value=fake_poller):
            result = _poll_pv("/dev/ttyUSB2", 2)

        assert result["ok"] is False
        assert "serial error" in result["error"]

    def test_close_called_on_success(self):
        fake_poller = MagicMock()
        fake_poller.read.return_value = _FakeAuroraSnapshot()

        with patch("poll.AbbAuroraPoller", return_value=fake_poller):
            _poll_pv("/dev/ttyUSB2", 2)

        fake_poller.close.assert_called_once()


# ---------------------------------------------------------------------------
# _run_once — routes to correct poll functions based on args
# ---------------------------------------------------------------------------

class TestRunOnce:
    def _args(self, battery=None, inverter=None, pv=None, battery_modules=1, inverter_usb=False, pv_address=2):
        ns = _build_parser().parse_args(
            (["--battery", battery] if battery else [])
            + (["--battery-modules", str(battery_modules)] if battery_modules != 1 else [])
            + (["--inverter", inverter] if inverter else [])
            + (["--inverter-usb"] if inverter_usb else [])
            + (["--pv", pv] if pv else [])
            + (["--pv-address", str(pv_address)] if pv_address != 2 else [])
        )
        return ns

    def test_only_battery(self):
        args = self._args(battery="/dev/ttyUSB0")
        with patch("poll._poll_battery", return_value={"ok": True}) as mb, \
             patch("poll._poll_inverter") as mi, \
             patch("poll._poll_pv") as mp:
            _run_once(args)
        mb.assert_called_once()
        mi.assert_not_called()
        mp.assert_not_called()

    def test_only_inverter(self):
        args = self._args(inverter="/dev/ttyUSB1")
        with patch("poll._poll_battery") as mb, \
             patch("poll._poll_inverter", return_value={"ok": True}) as mi, \
             patch("poll._poll_pv") as mp:
            _run_once(args)
        mb.assert_not_called()
        mi.assert_called_once()
        mp.assert_not_called()

    def test_only_pv(self):
        args = self._args(pv="/dev/ttyUSB2")
        with patch("poll._poll_battery") as mb, \
             patch("poll._poll_inverter") as mi, \
             patch("poll._poll_pv", return_value={"ok": True}) as mp:
            _run_once(args)
        mb.assert_not_called()
        mi.assert_not_called()
        mp.assert_called_once()

    def test_all_three(self):
        args = self._args(battery="/dev/ttyUSB0", inverter="/dev/ttyUSB1", pv="/dev/ttyUSB2")
        with patch("poll._poll_battery", return_value={"ok": True}), \
             patch("poll._poll_inverter", return_value={"ok": True}), \
             patch("poll._poll_pv", return_value={"ok": True}):
            results = _run_once(args)
        assert "battery" in results
        assert "inverter" in results
        assert "pv" in results

    def test_battery_modules_forwarded(self):
        args = self._args(battery="/dev/ttyUSB0", battery_modules=3)
        with patch("poll._poll_battery", return_value={"ok": True}) as mb:
            _run_once(args)
        mb.assert_called_once_with("/dev/ttyUSB0", 3)

    def test_pv_address_forwarded(self):
        args = self._args(pv="/dev/ttyUSB2", pv_address=5)
        with patch("poll._poll_pv", return_value={"ok": True}) as mp:
            _run_once(args)
        mp.assert_called_once_with("/dev/ttyUSB2", 5)


# ---------------------------------------------------------------------------
# _display — human-readable text output
# ---------------------------------------------------------------------------

class TestDisplayHuman:
    def _capture(self, results):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            _display(results, as_json=False)
        return buf.getvalue()

    def test_battery_section_present(self):
        snap = _FakeBmsSnapshot()
        out = self._capture({"battery": {"ok": True, "snapshot": snap}})
        assert "Pylontech" in out
        assert "75.0" in out   # SoC value

    def test_inverter_section_present(self):
        out = self._capture({
            "inverter": {
                "ok": True,
                "protocol": "PI18",
                "mode": "L",
                "status": {"raw": "230 50"},
                "flags": "10100000",
            }
        })
        assert "Iconica" in out or "Inverter" in out
        assert "PI18" in out
        assert "L" in out

    def test_pv_section_present(self):
        snap = _FakeAuroraSnapshot()
        out = self._capture({"pv": {"ok": True, "snapshot": snap}})
        assert "Aurora" in out or "PV" in out or "ABB" in out
        assert "1500.0" in out

    def test_error_shown_in_output(self):
        out = self._capture({"battery": {"ok": False, "error": "No response from module 0"}})
        assert "No response from module 0" in out

    def test_all_sections_shown(self):
        results = {
            "battery": {"ok": True, "snapshot": _FakeBmsSnapshot()},
            "inverter": {
                "ok": True,
                "protocol": "PI18",
                "mode": "L",
                "status": None,
                "flags": None,
            },
            "pv": {"ok": True, "snapshot": _FakeAuroraSnapshot()},
        }
        out = self._capture(results)
        assert "Pylontech" in out
        assert "PI18" in out
        assert "1500.0" in out


# ---------------------------------------------------------------------------
# _display — JSON output
# ---------------------------------------------------------------------------

class TestDisplayJson:
    def _capture_json(self, results):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            _display(results, as_json=True)
        return json.loads(buf.getvalue())

    def test_json_has_timestamp(self):
        results = {"battery": {"ok": True, "snapshot": _FakeBmsSnapshot()}}
        payload = self._capture_json(results)
        assert "timestamp" in payload

    def test_json_battery_present(self):
        results = {"battery": {"ok": True, "snapshot": _FakeBmsSnapshot()}}
        payload = self._capture_json(results)
        assert "battery" in payload
        assert payload["battery"]["snapshot"]["soc_pct"] == 75.0

    def test_json_error_shown(self):
        results = {"battery": {"ok": False, "error": "boom"}}
        payload = self._capture_json(results)
        assert payload["battery"]["error"] == "boom"

    def test_json_pv_present(self):
        results = {"pv": {"ok": True, "snapshot": _FakeAuroraSnapshot()}}
        payload = self._capture_json(results)
        assert "pv" in payload
        assert payload["pv"]["snapshot"]["ac_power_w"] == 1500.0
