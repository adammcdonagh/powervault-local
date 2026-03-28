#!/usr/bin/env python3
"""Hardware poll utility — query battery, inverter and/or PV inverter and print a
nicely formatted summary.

Usage
-----
Poll all three components (adjust port paths for your system):

    python poll.py \\
        --battery  /dev/tty.usbserial-0001 \\
        --inverter /dev/tty.usbserial-0002 \\
        --pv       /dev/tty.usbserial-0003

Poll only the Pylontech battery stack:

    python poll.py --battery /dev/tty.usbserial-0001

Poll only the Iconica inverter (using the USB HID port on the front panel):

    python poll.py --inverter /dev/hidraw0 --inverter-usb

Poll the inverter and the ABB PV inverter, and loop every 10 seconds:

    python poll.py \\
        --inverter /dev/tty.usbserial-0002 \\
        --pv       /dev/tty.usbserial-0003 \\
        --interval 10

Full argument reference
-----------------------
  --battery PORT          RS485 port for the Pylontech battery stack
  --battery-modules N     Number of Pylontech modules (default 1)
  --inverter PORT         Serial/USB port for the Iconica inverter (P18)
  --inverter-usb          Use USB HID mode for the inverter (default: serial)
  --pv PORT               RS485 port for the ABB Aurora PV inverter
  --pv-address N          Aurora RS485 address (default 2)
  --interval SECS         Repeat every N seconds (default: run once then exit)
  --json                  Emit raw JSON instead of the human-readable table
  --log-level LEVEL       Python logging level (default WARNING)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import time
from dataclasses import asdict
from typing import Any

# Hardware driver imports — guarded so the module loads cleanly without
# hardware libraries installed (tests mock at module level).
try:
    from pylontech_driver.bms import BmsPoller  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    BmsPoller = None  # type: ignore[assignment,misc]

try:
    from p18_serial.p18 import P18Inverter  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    P18Inverter = None  # type: ignore[assignment,misc]

try:
    from abb_aurora.aurora import AbbAuroraPoller  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    AbbAuroraPoller = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Terminal colour helpers (no external dependency)
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"


def _c(text: str, *codes: str, force: bool = False) -> str:
    """Wrap *text* in ANSI codes, but only when stdout is a TTY (or force=True)."""
    if not force and not sys.stdout.isatty():
        return text
    return "".join(codes) + text + _RESET


def _bar(value: float, total: float = 100.0, width: int = 20) -> str:
    """Return an ASCII progress bar string."""
    filled = max(0, min(width, round(value / total * width)))
    bar = "█" * filled + "░" * (width - filled)
    pct = value / total * 100
    if pct >= 80:
        colour = _GREEN
    elif pct >= 30:
        colour = _YELLOW
    else:
        colour = _RED
    return _c(f"[{bar}]", colour) + f" {value:.1f}/{total:.0f}"


# ---------------------------------------------------------------------------
# Section printers
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    width = 60
    print()
    print(_c("─" * width, _DIM))
    print(_c(f"  {title}", _BOLD, _CYAN))
    print(_c("─" * width, _DIM))


def _row(label: str, value: str, unit: str = "", indent: int = 2) -> None:
    pad = " " * indent
    label_col = f"{pad}{label}"
    print(f"{label_col:<35}{_c(value, _BOLD)}  {_c(unit, _DIM)}")


def _subheader(text: str) -> None:
    print(_c(f"  ── {text}", _DIM))


# ---------------------------------------------------------------------------
# Battery output
# ---------------------------------------------------------------------------

def _print_battery(snap: Any) -> None:
    _header("🔋  Pylontech Battery Stack")

    direction = ""
    if snap.battery_current_a > 0.1:
        direction = _c("  ▲ charging", _GREEN)
    elif snap.battery_current_a < -0.1:
        direction = _c("  ▼ discharging", _YELLOW)
    else:
        direction = _c("  ● idle", _DIM)

    _row("State of Charge", _bar(snap.soc_pct) + direction)
    _row("State of Health (avg)", f"{snap.soh_pct:.1f}", "%")
    _row("State of Health (min)", f"{snap.soh_pct_min:.1f}", "%")
    _row("Cycle count (max)", str(snap.cycle_count_max))
    print()
    _row("Pack voltage", f"{snap.battery_voltage_v:.3f}", "V")
    _row("Pack current", f"{snap.battery_current_a:+.3f}", "A  (+charge / −discharge)")
    _row("Pack power", f"{snap.battery_power_w:+.1f}", "W")
    print()
    _row("Cell voltage (max)", f"{snap.cell_voltage_max_v:.4f}", "V")
    _row("Cell voltage (min)", f"{snap.cell_voltage_min_v:.4f}", "V")
    _row("Cell temperature (avg)", f"{snap.cell_temp_avg_c:.1f}", "°C")
    _row("Cell temperature (max)", f"{snap.cell_temp_max_c:.1f}", "°C")
    _row("Cell temperature (min)", f"{snap.cell_temp_min_c:.1f}", "°C")

    if snap.charge_current_limit_a is not None:
        print()
        _subheader("BMS limits")
        _row("Max charge current", f"{snap.charge_current_limit_a:.1f}", "A")
        if snap.discharge_current_limit_a is not None:
            _row("Max discharge current", f"{snap.discharge_current_limit_a:.1f}", "A")
        if snap.charge_voltage_limit_v is not None:
            _row("Charge voltage limit", f"{snap.charge_voltage_limit_v:.2f}", "V")
        if snap.discharge_voltage_limit_v is not None:
            _row("Discharge voltage limit", f"{snap.discharge_voltage_limit_v:.2f}", "V")

    if len(snap.modules) > 1:
        print()
        _subheader("Per-module summary")
        for m in snap.modules:
            _row(
                f"  Module {m.address}",
                f"SoC {m.soc_pct:.0f}%  "
                f"{m.voltage_v:.2f}V  "
                f"{m.current_a:+.1f}A  "
                f"SoH {m.soh_pct:.0f}%  "
                f"cyc {m.cycle_count}",
            )


# ---------------------------------------------------------------------------
# Iconica inverter output
# ---------------------------------------------------------------------------

_MODE_NAMES = {
    "P": "Power-on",
    "S": "Standby",
    "L": "Line (grid)",
    "B": "Battery",
    "F": "Fault",
    "C": "Charge",
}


def _print_inverter(protocol: str | None, mode: str | None, status: dict | None, flags: str | None) -> None:
    _header("⚡  Iconica Inverter (Voltronic InfiniSolar / P18)")

    _row("Protocol", protocol or _c("unknown", _DIM))

    if mode:
        mode_str = _MODE_NAMES.get(mode, mode)
        colour = _GREEN if mode == "L" else (_YELLOW if mode in ("B", "C") else _RED if mode == "F" else _DIM)
        _row("Operating mode", _c(f"{mode}  ({mode_str})", colour))
    else:
        _row("Operating mode", _c("no response", _RED))

    if flags:
        _row("Device flags", flags)

    if status and status.get("raw"):
        raw = status["raw"]
        fields = raw.split()
        _subheader("General status (raw fields)")
        # P18 GS response: space-delimited; field meaning depends on firmware.
        # Print each field with its index for identification.
        for i, f in enumerate(fields):
            _row(f"  Field {i:02d}", f)


# ---------------------------------------------------------------------------
# ABB Aurora PV inverter output
# ---------------------------------------------------------------------------

def _print_pv(snap: Any) -> None:
    _header("☀️   ABB UNO PVI Solar Inverter (Aurora)")

    state_colour = _GREEN if snap.is_producing else _DIM
    _row("State", _c(snap.inverter_state or "unknown", state_colour))
    _row("Producing", _c("YES", _GREEN) if snap.is_producing else _c("NO", _DIM))
    print()
    _row("AC output power", f"{snap.ac_power_w:.1f}", "W")
    _row("DC input power", f"{snap.dc_power_w:.1f}", "W")
    if snap.dc_voltage_v is not None:
        _row("DC voltage (panels)", f"{snap.dc_voltage_v:.2f}", "V")
    if snap.dc_current_a is not None:
        _row("DC current (panels)", f"{snap.dc_current_a:.3f}", "A")
    if snap.grid_voltage_v is not None:
        _row("Grid voltage", f"{snap.grid_voltage_v:.2f}", "V")
    if snap.grid_frequency_hz is not None:
        _row("Grid frequency", f"{snap.grid_frequency_hz:.2f}", "Hz")
    if snap.temperature_c is not None:
        _row("Inverter temperature", f"{snap.temperature_c:.1f}", "°C")
    if snap.energy_today_kwh is not None:
        _row("Energy today", f"{snap.energy_today_kwh:.3f}", "kWh")
    if snap.energy_total_kwh is not None:
        _row("Energy total (lifetime)", f"{snap.energy_total_kwh:.3f}", "kWh")


# ---------------------------------------------------------------------------
# Poll functions
# ---------------------------------------------------------------------------

def _poll_battery(port: str, num_modules: int) -> dict:
    if BmsPoller is None:  # pragma: no cover
        return {"ok": False, "error": "python-pylontech is not installed (pip install python-pylontech)"}

    poller = BmsPoller(port=port, num_modules=num_modules)
    try:
        snap = poller.read()
        return {"ok": True, "snapshot": snap}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        poller.close()


def _poll_inverter(port: str, is_usb: bool) -> dict:
    if P18Inverter is None:  # pragma: no cover
        return {"ok": False, "error": "pyserial is not installed (pip install pyserial)"}

    try:
        with P18Inverter(port=port, is_usb=is_usb) as inv:
            protocol = inv.get_protocol_id()
            mode = inv.get_mode()
            status = inv.get_general_status()
            flags = inv.get_device_flags()
        return {"ok": True, "protocol": protocol, "mode": mode, "status": status, "flags": flags}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _poll_pv(port: str, address: int) -> dict:
    if AbbAuroraPoller is None:  # pragma: no cover
        return {"ok": False, "error": "aurorapy is not installed (pip install aurorapy)"}

    poller = AbbAuroraPoller(port=port, address=address)
    try:
        snap = poller.read()
        return {"ok": True, "snapshot": snap}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        poller.close()


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def _to_json_safe(obj: Any) -> Any:
    """Recursively convert dataclasses and other objects to JSON-serialisable form."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_json_safe(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    return obj


def _print_json(results: dict) -> None:
    payload: dict[str, Any] = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for key, result in results.items():
        if result["ok"]:
            snap_or_data = {k: _to_json_safe(v) for k, v in result.items() if k != "ok"}
            payload[key] = snap_or_data
        else:
            payload[key] = {"error": result["error"]}
    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poll",
        description=textwrap.dedent("""\
            Poll Powervault P3 hardware directly over USB/RS485 and print a
            formatted summary of battery, inverter and/or PV inverter metrics.

            At least one of --battery, --inverter, or --pv must be supplied.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Battery ---
    batt = parser.add_argument_group("Pylontech battery stack")
    batt.add_argument(
        "--battery",
        metavar="PORT",
        help="RS485 serial port connected to the Pylontech master battery "
             "(e.g. /dev/tty.usbserial-0001  or  /dev/ttyUSB1)",
    )
    batt.add_argument(
        "--battery-modules",
        type=int,
        default=1,
        metavar="N",
        help="Number of Pylontech modules in the stack (default: 1)",
    )

    # --- Inverter ---
    inv = parser.add_argument_group("Iconica inverter (P18 / Voltronic InfiniSolar)")
    inv.add_argument(
        "--inverter",
        metavar="PORT",
        help="Serial/USB port for the Iconica inverter "
             "(e.g. /dev/tty.usbserial-0002  or  /dev/ttyUSB0  or  /dev/hidraw0)",
    )
    inv.add_argument(
        "--inverter-usb",
        action="store_true",
        help="Use USB HID mode (for the USB port on the inverter front panel). "
             "Default is RS-232 serial mode.",
    )

    # --- PV inverter ---
    pv = parser.add_argument_group("ABB UNO PVI solar inverter (Aurora)")
    pv.add_argument(
        "--pv",
        metavar="PORT",
        help="RS485 serial port connected to the ABB Aurora inverter "
             "(e.g. /dev/tty.usbserial-0003  or  /dev/ttyUSB2)",
    )
    pv.add_argument(
        "--pv-address",
        type=int,
        default=2,
        metavar="N",
        help="Aurora RS485 device address (default: 2)",
    )

    # --- General options ---
    gen = parser.add_argument_group("General options")
    gen.add_argument(
        "--interval",
        type=float,
        default=0,
        metavar="SECS",
        help="Poll repeatedly every N seconds. Omit (or use 0) to poll once.",
    )
    gen.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable display.",
    )
    gen.add_argument(
        "--log-level",
        default="WARNING",
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Python logging level (default: WARNING)",
    )

    return parser


def _run_once(args: argparse.Namespace) -> dict:
    """Collect readings for all requested components and return a results dict."""
    results: dict[str, Any] = {}

    if args.battery:
        results["battery"] = _poll_battery(args.battery, args.battery_modules)

    if args.inverter:
        results["inverter"] = _poll_inverter(args.inverter, args.inverter_usb)

    if args.pv:
        results["pv"] = _poll_pv(args.pv, args.pv_address)

    return results


def _display(results: dict, as_json: bool) -> None:
    if as_json:
        _print_json(results)
        return

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(_c(f"\nPoll at {ts}", _DIM))

    if "battery" in results:
        r = results["battery"]
        if r["ok"]:
            _print_battery(r["snapshot"])
        else:
            _header("🔋  Pylontech Battery Stack")
            print(_c(f"  ERROR: {r['error']}", _RED))

    if "inverter" in results:
        r = results["inverter"]
        if r["ok"]:
            _print_inverter(r["protocol"], r["mode"], r["status"], r["flags"])
        else:
            _header("⚡  Iconica Inverter")
            print(_c(f"  ERROR: {r['error']}", _RED))

    if "pv" in results:
        r = results["pv"]
        if r["ok"]:
            _print_pv(r["snapshot"])
        else:
            _header("☀️   ABB Aurora PV Inverter")
            print(_c(f"  ERROR: {r['error']}", _RED))

    print()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not any([args.battery, args.inverter, args.pv]):
        parser.error("At least one of --battery, --inverter, or --pv must be specified.")

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    )

    try:
        if args.interval and args.interval > 0:
            while True:
                results = _run_once(args)
                _display(results, args.json)
                time.sleep(args.interval)
        else:
            results = _run_once(args)
            _display(results, args.json)
    except KeyboardInterrupt:
        print("\nInterrupted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
