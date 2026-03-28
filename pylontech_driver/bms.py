"""Pylontech US2000C battery BMS driver over RS485.

Polls each battery module directly using the python-pylontech library,
which speaks the documented Pylontech Low Voltage RS485 protocol.

Hardware connection
-------------------
USB-to-RS485 adapter → RJ11/RJ12 on the master Pylontech battery
  Pin 7 = RS485-A (positive)
  Pin 8 = RS485-B (negative)

DIP switch 1 on the master battery unit must be set to OFF to select
115200 baud (the library default). All other batteries in the stack
have DIP switch 1 = ON (9600 baud for inter-battery communication, but
the master handles the RS485 bus so this only affects the internal link).

The "Contact" and "Console" ports on each battery are NOT needed here:
  - Console: RS232 CLI for diagnostics/firmware only
  - Contact: dry-relay for simple alarm signalling only
  - RS485: BMS data bus – this is the one we use

Module addresses are set by the DIP switches on each battery. The master
is address 0; slaves are 1, 2, … N-1.

Usage
-----
    from pylontech_driver.bms import BmsPoller

    poller = BmsPoller(port="/dev/ttyUSB1", num_modules=2)
    snapshot = poller.read()
    print(snapshot.soc_pct, snapshot.battery_voltage_v)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ModuleData:
    """Data for a single Pylontech battery module."""

    address: int
    voltage_v: float
    current_a: float  # positive = charging, negative = discharging
    temperature_c: float
    soc_pct: float
    soh_pct: float
    cycle_count: int
    cell_voltages_v: list[float] = field(default_factory=list)
    temperatures_c: list[float] = field(default_factory=list)
    # BMS protection limits (from system parameters response)
    charge_voltage_limit_v: Optional[float] = None
    discharge_voltage_limit_v: Optional[float] = None
    charge_current_limit_a: Optional[float] = None
    discharge_current_limit_a: Optional[float] = None


@dataclass
class BmsSnapshot:
    """Aggregated snapshot across all modules in the battery stack."""

    # Per-module data (index = address)
    modules: list[ModuleData] = field(default_factory=list)

    # Stack-level aggregates (calculated from module data)
    soc_pct: float = 0.0  # average across modules
    soh_pct: float = 0.0  # average
    soh_pct_min: float = 0.0  # worst module
    cycle_count_avg: float = 0.0
    cycle_count_max: int = 0  # highest cycled module

    battery_voltage_v: float = 0.0  # average module voltage
    battery_current_a: float = 0.0  # sum (total pack current)
    battery_power_w: float = 0.0  # derived: voltage × current

    cell_voltage_max_v: float = 0.0
    cell_voltage_min_v: float = 0.0
    cell_temp_avg_c: float = 0.0
    cell_temp_max_c: float = 0.0
    cell_temp_min_c: float = 0.0

    # BMS limits (most restrictive across all modules)
    charge_voltage_limit_v: Optional[float] = None
    discharge_voltage_limit_v: Optional[float] = None
    charge_current_limit_a: Optional[float] = None
    discharge_current_limit_a: Optional[float] = None


def _aggregate(modules: list[ModuleData]) -> BmsSnapshot:
    """Compute stack-level aggregates from a list of module readings."""
    if not modules:
        return BmsSnapshot()

    snap = BmsSnapshot(modules=modules)

    n = len(modules)
    snap.soc_pct = sum(m.soc_pct for m in modules) / n
    snap.soh_pct = sum(m.soh_pct for m in modules) / n
    snap.soh_pct_min = min(m.soh_pct for m in modules)
    snap.cycle_count_avg = sum(m.cycle_count for m in modules) / n
    snap.cycle_count_max = max(m.cycle_count for m in modules)

    snap.battery_voltage_v = sum(m.voltage_v for m in modules) / n
    snap.battery_current_a = sum(m.current_a for m in modules)
    snap.battery_power_w = snap.battery_voltage_v * snap.battery_current_a

    all_cells = [v for m in modules for v in m.cell_voltages_v]
    if all_cells:
        snap.cell_voltage_max_v = max(all_cells)
        snap.cell_voltage_min_v = min(all_cells)

    all_temps = [t for m in modules for t in m.temperatures_c]
    if all_temps:
        snap.cell_temp_avg_c = sum(all_temps) / len(all_temps)
        snap.cell_temp_max_c = max(all_temps)
        snap.cell_temp_min_c = min(all_temps)

    # BMS limits: use most restrictive (lowest charge limit, highest discharge limit)
    cv_limits = [
        m.charge_voltage_limit_v
        for m in modules
        if m.charge_voltage_limit_v is not None
    ]
    if cv_limits:
        snap.charge_voltage_limit_v = min(cv_limits)

    dv_limits = [
        m.discharge_voltage_limit_v
        for m in modules
        if m.discharge_voltage_limit_v is not None
    ]
    if dv_limits:
        snap.discharge_voltage_limit_v = max(dv_limits)

    ci_limits = [
        m.charge_current_limit_a
        for m in modules
        if m.charge_current_limit_a is not None
    ]
    if ci_limits:
        snap.charge_current_limit_a = min(ci_limits)

    di_limits = [
        m.discharge_current_limit_a
        for m in modules
        if m.discharge_current_limit_a is not None
    ]
    if di_limits:
        snap.discharge_current_limit_a = min(di_limits)

    return snap


class BmsPoller:
    """Poll all Pylontech modules on the RS485 bus and return a BmsSnapshot.

    Parameters
    ----------
    port:
        Serial port connected to the RS485 bus (e.g. ``/dev/ttyUSB1``).
    num_modules:
        Number of battery modules in the stack (default 1). Modules are
        addressed 0 … num_modules-1.
    baudrate:
        RS485 baud rate – must match DIP switch 1 setting on the master
        battery (OFF = 115200, which is the default here and for the
        python-pylontech library).
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB1",
        num_modules: int = 1,
        baudrate: int = 115200,
    ) -> None:
        self.port = port
        self.num_modules = num_modules
        self.baudrate = baudrate
        self._pylontech: object | None = None

    def _connect(self) -> None:
        """Lazy-initialise the pylontech serial connection."""
        try:
            import pylontech  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "python-pylontech is required: pip install python-pylontech"
            ) from exc

        self._pylontech = pylontech.Pylontech(
            serial_port=self.port,
            baudrate=self.baudrate,
        )
        logger.info("Connected to Pylontech RS485 bus on %s", self.port)

    def read(self) -> BmsSnapshot:
        """Read all modules and return an aggregated BmsSnapshot.

        Raises
        ------
        RuntimeError
            If communication with the battery fails.
        """
        if self._pylontech is None:
            self._connect()

        modules: list[ModuleData] = []

        for addr in range(self.num_modules):
            module = self._read_module(addr)
            if module is not None:
                modules.append(module)

        if not modules:
            raise RuntimeError("No battery modules responded on RS485 bus")

        return _aggregate(modules)

    def _read_module(self, address: int) -> ModuleData | None:
        """Read a single module by address. Returns None on communication error."""
        try:
            info = self._pylontech.get_values_single(address)  # type: ignore[union-attr]
            params = self._pylontech.get_system_parameters()  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Failed to read Pylontech module %d: %s", address, exc)
            return None

        try:
            cell_voltages = [v / 1000.0 for v in (info.CellVoltages or [])]
            temperatures = [t / 1000.0 for t in (info.Temperatures or [])]

            module = ModuleData(
                address=address,
                voltage_v=info.Voltage / 1000.0,
                current_a=info.Current / 1000.0,
                temperature_c=info.Temperature / 1000.0,
                soc_pct=info.RSOC,
                soh_pct=info.RSOH if hasattr(info, "RSOH") else 100.0,
                cycle_count=info.CycleNumber if hasattr(info, "CycleNumber") else 0,
                cell_voltages_v=cell_voltages,
                temperatures_c=temperatures,
            )

            if params is not None:
                module.charge_voltage_limit_v = getattr(
                    params, "ChargeVoltageLimit", None
                )
                module.discharge_voltage_limit_v = getattr(
                    params, "DischargeVoltageLimit", None
                )
                cl = getattr(params, "ChargeCurrentLimit", None)
                module.charge_current_limit_a = cl / 1000.0 if cl is not None else None
                dl = getattr(params, "DischargeCurrentLimit", None)
                module.discharge_current_limit_a = (
                    dl / 1000.0 if dl is not None else None
                )

            logger.debug(
                "Module %d: %.2fV %.1fA SoC=%.1f%% SoH=%.1f%%",
                address,
                module.voltage_v,
                module.current_a,
                module.soc_pct,
                module.soh_pct,
            )
            return module

        except Exception as exc:
            logger.warning("Failed to parse Pylontech module %d data: %s", address, exc)
            return None

    def close(self) -> None:
        """Close the serial connection."""
        if self._pylontech is not None:
            try:
                self._pylontech.close()  # type: ignore[union-attr]
            except Exception:
                pass
            self._pylontech = None

    def __enter__(self) -> "BmsPoller":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
