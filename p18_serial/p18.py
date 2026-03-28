"""
Voltronic P18 serial protocol driver for the Iconica / InfiniSolar E 5.5 kW.

The Powervault P3's M4 controller communicates with the inverter over a
serial link (RS-232, 2400 baud 8N1) using the Voltronic P18 protocol.

**Connection options:**
  1. Tap the existing M4 ↔ inverter serial line (see HARDWARE_NOTES.md).
  2. Connect directly to the inverter's own RS-232 / USB port if exposed.
  3. Intercept via a serial proxy (pass-through + logging / injection).

**Protocol structure (P18):**

  Command frame::

    ^<type><length><command><CRC_high><CRC_low><CR>

  Where:
    ^       = literal caret (0x5E)
    type    = P (query) or S (setting command)
    length  = zero-padded 3-digit decimal: len(command) + 1  (+1 for CR)
    command = ASCII command string
    CRC     = CRC-XMODEM (polynomial 0x1021) over the entire ``^type+length+command`` bytes
    CR      = 0x0D

  Response frame::

    (<data><CRC_high><CRC_low><CR>

  A NAK looks like: ``(NAK<CRC><CR>``

**Important caveats:**
  - Some commands listed here are derived from Voltronic community
    documentation and the reference local-control guide; they should be
    verified against your specific firmware before relying on them.
  - Direct serial access bypasses all M4 safety logic. Use with care.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CRC-XMODEM
# ---------------------------------------------------------------------------

def crc16_xmodem(data: bytes) -> int:
    """Calculate CRC-16/XMODEM (polynomial 0x1021, init 0x0000)."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Frame encoding / decoding
# ---------------------------------------------------------------------------

def encode_command(cmd: str, cmd_type: str = "P") -> bytes:
    """Encode a P18 command into a wire-ready byte string.

    Parameters
    ----------
    cmd:
        The command string (e.g. ``"GS"``, ``"POP00"``).
    cmd_type:
        ``"P"`` for query commands, ``"S"`` for set/control commands.

    Returns
    -------
    bytes
        Complete frame including CRC and CR terminator.
    """
    if cmd_type not in ("P", "S"):
        raise ValueError(f"cmd_type must be 'P' or 'S', got {cmd_type!r}")

    length = len(cmd) + 1  # +1 for the trailing CR included in length
    header = f"^{cmd_type}{length:03d}{cmd}"
    header_bytes = header.encode("ascii")
    crc = crc16_xmodem(header_bytes)
    return header_bytes + struct.pack(">H", crc) + b"\r"


def decode_response(raw: bytes) -> str | None:
    """Validate CRC and return the data portion of a P18 response.

    Returns *None* if the CRC is invalid or the frame is malformed.
    The leading ``(`` and trailing ``<CRC><CR>`` are stripped.
    """
    # Minimum valid response: ( + at least 0 data bytes + 2 CRC bytes + CR = 4
    if len(raw) < 4:
        logger.debug("Response too short (%d bytes): %r", len(raw), raw)
        return None

    # Strip trailing CR
    if raw.endswith(b"\r"):
        raw = raw[:-1]

    # Last 2 bytes are CRC
    frame_data = raw[:-2]
    recv_crc = struct.unpack(">H", raw[-2:])[0]
    calc_crc = crc16_xmodem(frame_data)

    if recv_crc != calc_crc:
        logger.warning(
            "CRC mismatch: received 0x%04X, calculated 0x%04X for %r",
            recv_crc,
            calc_crc,
            frame_data,
        )
        return None

    # Strip leading '(' acknowledgement byte
    decoded = frame_data.decode("ascii", errors="replace")
    if decoded.startswith("("):
        decoded = decoded[1:]

    return decoded


# ---------------------------------------------------------------------------
# High-level driver
# ---------------------------------------------------------------------------

class P18Inverter:
    """Driver for the Voltronic InfiniSolar E (P18 protocol) over RS-232.

    Parameters
    ----------
    port:
        Serial port path (e.g. ``/dev/ttyUSB0``) or ``/dev/hidraw0`` for USB
        HID. Use ``is_usb=True`` for HID connections.
    baud_rate:
        Serial baud rate — typically 2400 for P18.
    timeout:
        Read timeout in seconds.
    is_usb:
        When *True*, uses raw file I/O suitable for USB HID devices.
        When *False*, uses ``pyserial``.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baud_rate: int = 2400,
        timeout: float = 2.0,
        is_usb: bool = False,
    ) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.is_usb = is_usb
        self._dev: Any = None

    def connect(self) -> None:
        """Open the serial / HID connection."""
        if self.is_usb:
            self._dev = open(self.port, "r+b", buffering=0)
        else:
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError(
                    "pyserial is required for serial connections. "
                    "Install it with: pip install pyserial"
                ) from exc
            self._dev = serial.Serial(
                self.port,
                baudrate=self.baud_rate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self.timeout,
            )
        logger.info("Connected to inverter on %s", self.port)

    def disconnect(self) -> None:
        """Close the connection."""
        if self._dev:
            self._dev.close()
            self._dev = None

    def __enter__(self) -> "P18Inverter":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Low-level send / receive
    # ------------------------------------------------------------------

    def _send(self, cmd: str, cmd_type: str = "P") -> str | None:
        """Send *cmd* and return the decoded response data, or *None* on error."""
        if self._dev is None:
            raise RuntimeError("Not connected. Call connect() first.")

        frame = encode_command(cmd, cmd_type)
        logger.debug("TX: %r", frame)

        if self.is_usb:
            # USB HID requires 8-byte chunks
            for i in range(0, len(frame), 8):
                packet = frame[i : i + 8].ljust(8, b"\x00")
                self._dev.write(packet)
            time.sleep(0.1)
            raw = b""
            while True:
                chunk = self._dev.read(8)
                if not chunk:
                    break
                raw += chunk
                if b"\r" in chunk:
                    break
        else:
            self._dev.write(frame)
            raw = self._dev.read_until(b"\r")

        logger.debug("RX raw: %r", raw)
        result = decode_response(raw)
        if result is None:
            logger.warning("Invalid response for command %r: %r", cmd, raw)
        return result

    # ------------------------------------------------------------------
    # Query commands
    # ------------------------------------------------------------------

    def get_protocol_id(self) -> str | None:
        """Return the inverter protocol identifier (e.g. ``"PI18"``).

        Command: ``^P003PI``
        """
        return self._send("PI")

    def get_mode(self) -> str | None:
        """Return the current operating mode character.

        Command: ``^P004MOD``

        Returns one of: P=Power-on, S=Standby, L=Line, B=Battery,
        F=Fault, C=Charge.
        """
        return self._send("MOD")

    def get_general_status(self) -> dict | None:
        """Return a parsed general-status dict.

        Command: ``^P003GS``

        The response is a space-delimited string whose fields vary by firmware.
        Returns the raw string in ``{"raw": ...}`` so callers can parse further.
        """
        raw = self._send("GS")
        if raw is None:
            return None
        return {"raw": raw}

    def get_device_flags(self) -> str | None:
        """Return device flags / enabled features.

        Command: ``^P005FLAG``
        """
        return self._send("FLAG")

    # ------------------------------------------------------------------
    # Control commands (require direct serial access to the inverter)
    # ------------------------------------------------------------------

    def set_output_priority(self, priority: str) -> bool:
        """Set the output source priority.

        Parameters
        ----------
        priority:
            One of ``"UTI"`` (utility/grid first), ``"SOL"`` (solar first),
            ``"SBU"`` (solar → battery → utility).

        Returns *True* on acknowledgement, *False* on NAK or error.

        Command: ``^S006POP<code>``
        """
        codes = {"UTI": "00", "SOL": "01", "SBU": "02"}
        if priority not in codes:
            raise ValueError(f"priority must be one of {list(codes)}")
        result = self._send(f"POP{codes[priority]}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Output priority set to %s", priority)
        else:
            logger.warning("set_output_priority(%s) returned: %r", priority, result)
        return ok

    def set_charger_priority(self, priority: str) -> bool:
        """Set the charger source priority.

        Parameters
        ----------
        priority:
            One of ``"UTI"`` (grid first), ``"SOL"`` (solar first),
            ``"MIX"`` (solar + grid), ``"ONL"`` (solar only).

        Command: ``^S006PCP<code>``
        """
        codes = {"UTI": "00", "SOL": "01", "MIX": "02", "ONL": "03"}
        if priority not in codes:
            raise ValueError(f"priority must be one of {list(codes)}")
        result = self._send(f"PCP{codes[priority]}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Charger priority set to %s", priority)
        else:
            logger.warning("set_charger_priority(%s) returned: %r", priority, result)
        return ok

    def set_max_charge_current(self, amps: int) -> bool:
        """Set total maximum charging current in amps.

        Command: ``^S010MUCHGC<amps>`` (zero-padded to 3 digits).
        """
        result = self._send(f"MUCHGC{amps:03d}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Max charge current set to %dA", amps)
        return ok

    def set_ac_charge_current(self, amps: int) -> bool:
        """Set AC (grid) charging current in amps.

        Command: ``^S010MCHGC<amps>`` (zero-padded to 3 digits).
        """
        result = self._send(f"MCHGC{amps:03d}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("AC charge current set to %dA", amps)
        return ok

    def set_battery_recharge_voltage(self, volts: float) -> bool:
        """Set the battery voltage at which grid charging starts.

        Command: ``^S008PBCV<volts>``
        """
        result = self._send(f"PBCV{volts:.1f}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Battery re-charge voltage set to %.1fV", volts)
        return ok

    def set_battery_redischarge_voltage(self, volts: float) -> bool:
        """Set the battery voltage at which the inverter returns to battery mode.

        Command: ``^S008PBDV<volts>``
        """
        result = self._send(f"PBDV{volts:.1f}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Battery re-discharge voltage set to %.1fV", volts)
        return ok

    def set_battery_cutoff_voltage(self, volts: float) -> bool:
        """Set the low-voltage disconnect cutoff.

        Command: ``^S009PSDV<volts>``
        """
        result = self._send(f"PSDV{volts:.1f}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info("Battery cutoff voltage set to %.1fV", volts)
        return ok

    def set_battery_discharge_control(
        self,
        grid_charge: bool = True,
        discharge_to_load: bool = True,
        feed_in_to_grid: bool = False,
    ) -> bool:
        """Fine-grained battery charge/discharge control flags.

        Parameters
        ----------
        grid_charge:
            Allow charging from the AC grid.
        discharge_to_load:
            Allow discharging to power the house load.
        feed_in_to_grid:
            Allow exporting battery power to the grid.

        Command: ``^S010BATCD<a><b><c>``
        """
        flags = f"{int(grid_charge)}{int(discharge_to_load)}{int(feed_in_to_grid)}"
        result = self._send(f"BATCD{flags}", cmd_type="S")
        ok = result is not None and "NAK" not in result
        if ok:
            logger.info(
                "Battery discharge control: grid_charge=%s, discharge_to_load=%s, feed_in=%s",
                grid_charge, discharge_to_load, feed_in_to_grid,
            )
        return ok

    # ------------------------------------------------------------------
    # High-level mode helpers
    # ------------------------------------------------------------------

    def force_charge(self, ac_amps: int | None = None) -> None:
        """Force immediate charging from the grid.

        Sets output priority to grid and charger priority to grid.
        Optionally overrides the AC charging current.
        """
        self.set_output_priority("UTI")
        self.set_charger_priority("UTI")
        self.set_battery_discharge_control(grid_charge=True, discharge_to_load=False)
        if ac_amps is not None:
            self.set_ac_charge_current(ac_amps)
        logger.info("Force-charge mode activated")

    def normal_mode(self) -> None:
        """Return to normal solar/battery priority operation."""
        self.set_output_priority("SBU")
        self.set_charger_priority("SOL")
        self.set_battery_discharge_control(grid_charge=False, discharge_to_load=True)
        logger.info("Normal (SBU) mode activated")

    def idle_mode(self) -> None:
        """Hold battery — neither charge from grid nor discharge to load."""
        self.set_battery_discharge_control(
            grid_charge=False,
            discharge_to_load=False,
            feed_in_to_grid=False,
        )
        logger.info("Idle (hold) mode activated")
