"""ABB UNO PVI 3.0 solar inverter driver using the Aurora RS485 protocol.

ABB (formerly Power-One) solar inverters communicate using a proprietary
binary protocol known as the "Aurora" protocol. This module wraps the
``aurorapy`` Python library to poll the inverter for PV production data.

Hardware connection
-------------------
USB-to-RS485 adapter → RS485 terminals on the ABB UNO PVI 3.0
  The inverter's RS485 connector is typically a 3-pin or RJ45 terminal:
    A (positive) / B (negative) / GND

Communication parameters
------------------------
  Baud rate : 19200
  Data bits : 8
  Parity    : None
  Stop bits : 1
  Inverter default RS485 address : 2

The ABB UNO PVI 3.0 does NOT need the optional PVI-RS485-MODBUS gateway —
that device translates Aurora → Modbus for third-party SCADA systems. We
speak Aurora directly via ``aurorapy``.

Available data
--------------
The Aurora protocol exposes:
  - DC input power (PV panel watts)
  - AC output power (watts injected to grid/house)
  - Grid voltage and frequency
  - Inverter temperature
  - Daily and cumulative energy (kWh)
  - Inverter state / fault codes

Usage
-----
    from abb_aurora.aurora import AbbAuroraPoller

    poller = AbbAuroraPoller(port="/dev/ttyUSB2", address=2)
    snapshot = poller.read()
    print(snapshot.ac_power_w, snapshot.dc_power_w)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Aurora inverter state codes → human-readable strings
_INVERTER_STATES = {
    0: "Stand-by",
    1: "Checking grid",
    2: "Run",
    3: "Bulk",
    4: "Out",
    5: "Constant voltage",
    6: "Charge",
    7: "BGA",
    8: "Lock out",
    9: "PV off",
    10: "No parameters",
    11: "Bulk under",
    12: "Out over",
    13: "Comm error",
    14: "Converters error",
    15: "No grid",
    16: "Bulk low",
    17: "PV low",
    18: "Manual off",
    19: "Waiting for sun",
    20: "Temperature fault",
    21: "Fan stalled",
    22: "Int comm fault",
}


@dataclass
class AuroraSnapshot:
    """Data snapshot from the ABB UNO PVI solar inverter."""

    # Power
    ac_power_w: float = 0.0     # AC output power (watts)
    dc_power_w: float = 0.0     # DC input power from PV panels (watts)

    # DC input (panel side)
    dc_voltage_v: Optional[float] = None
    dc_current_a: Optional[float] = None

    # AC output (grid side)
    grid_voltage_v: Optional[float] = None
    grid_frequency_hz: Optional[float] = None

    # Thermal
    temperature_c: Optional[float] = None

    # Energy counters
    energy_today_kwh: Optional[float] = None
    energy_total_kwh: Optional[float] = None

    # Status
    inverter_state: Optional[str] = None
    is_producing: bool = False


class AbbAuroraPoller:
    """Poll an ABB UNO PVI inverter using the Aurora RS485 protocol.

    Parameters
    ----------
    port:
        Serial port connected to the RS485 bus (e.g. ``/dev/ttyUSB2``).
    address:
        RS485 device address of the inverter (factory default is 2).
    baudrate:
        Baud rate – ABB Aurora inverters use 19200 by default.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB2",
        address: int = 2,
        baudrate: int = 19200,
    ) -> None:
        self.port = port
        self.address = address
        self.baudrate = baudrate
        self._client: object | None = None

    def _connect(self) -> None:
        """Lazy-initialise the aurorapy serial client."""
        try:
            from aurorapy.client import AuroraSerialClient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "aurorapy is required: pip install aurorapy"
            ) from exc

        self._client = AuroraSerialClient(
            self.port,
            address=self.address,
            baudrate=self.baudrate,
        )
        self._client.connect()  # type: ignore[union-attr]
        logger.info(
            "Connected to ABB Aurora inverter on %s (address %d)",
            self.port,
            self.address,
        )

    def read(self) -> AuroraSnapshot:
        """Read current data from the inverter and return an AuroraSnapshot.

        Returns a snapshot with zero/None values if the inverter is offline
        (e.g. after dark when the inverter powers down). Raises RuntimeError
        only if the serial port itself cannot be opened.
        """
        if self._client is None:
            self._connect()

        snap = AuroraSnapshot()

        snap.dc_voltage_v = self._safe_read("measure", None, "v_in_1")
        snap.dc_current_a = self._safe_read("measure", None, "i_in_1")
        snap.grid_voltage_v = self._safe_read("measure", None, "v_grid")
        snap.grid_frequency_hz = self._safe_read("measure", None, "frequency")
        snap.temperature_c = self._safe_read("measure", None, "temperature_inverter")
        snap.energy_today_kwh = self._safe_read_energy("day")
        snap.energy_total_kwh = self._safe_read_energy("total")

        # Read AC and DC power using the dedicated DSP measurement values
        ac_dsp = self._safe_read("measure", None, "power_grid")
        dc_dsp = self._safe_read("measure", None, "power_in")
        if ac_dsp is not None:
            snap.ac_power_w = ac_dsp
        if dc_dsp is not None:
            snap.dc_power_w = dc_dsp

        snap.is_producing = snap.ac_power_w > 5.0

        state_code = self._safe_read_state()
        if state_code is not None:
            snap.inverter_state = _INVERTER_STATES.get(state_code, f"Unknown ({state_code})")

        logger.debug(
            "ABB Aurora: AC=%.1fW DC=%.1fW temp=%s°C state=%s",
            snap.ac_power_w,
            snap.dc_power_w,
            snap.temperature_c,
            snap.inverter_state,
        )
        return snap

    def _safe_read(self, method: str, default: object, *args: object) -> object:
        """Call a client method, returning default on any communication error."""
        try:
            return getattr(self._client, method)(*args)  # type: ignore[union-attr]
        except Exception as exc:
            # Inverter may be offline after sunset — treat as non-fatal
            logger.debug("Aurora read %s%s: %s", method, args, exc)
            return default

    def _safe_read_energy(self, period: str) -> Optional[float]:
        """Read cumulated energy in kWh for the given period ('day' or 'total')."""
        try:
            wh = self._client.cumulated_energy(period)  # type: ignore[union-attr]
            return wh / 1000.0 if wh is not None else None
        except Exception as exc:
            logger.debug("Aurora energy(%s): %s", period, exc)
            return None

    def _safe_read_state(self) -> Optional[int]:
        """Read the global inverter state code."""
        try:
            return self._client.state()  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug("Aurora state(): %s", exc)
            return None

    def close(self) -> None:
        """Close the serial connection."""
        if self._client is not None:
            try:
                self._client.close()  # type: ignore[union-attr]
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "AbbAuroraPoller":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
