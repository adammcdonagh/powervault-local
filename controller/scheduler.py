"""Time-of-use scheduling logic for the Powervault P3.

Determines the desired battery mode based on:
  1. Time-of-use electricity tariff windows
  2. Current battery SoC
  3. Current solar PV production
  4. Current house consumption

Tariff windows and charge targets are configurable via environment
variables so they can be adjusted without code changes.

Schedule modes (matching the existing P3/p18_serial vocabulary):
  idle          – hold battery, neither charge nor discharge
  charge        – grid-charge battery (during cheap tariff)
  discharge     – discharge battery to supply the house
  force_charge  – maximum grid charge regardless of conditions
  force_discharge – discharge as fast as possible (e.g. during export)

Environment variables
---------------------
CHEAP_START   HH:MM  start of cheap tariff window (default 00:30)
CHEAP_END     HH:MM  end of cheap tariff window (default 04:30)
CHARGE_TARGET float  SoC % to reach by end of cheap window (default 90)
MIN_SOC       float  SoC % below which we always discharge from grid (default 15)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time

from pylontech_driver.bms import BmsSnapshot
from abb_aurora.aurora import AuroraSnapshot

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str, default: time) -> time:
    try:
        parts = value.strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except Exception:
        logger.warning("Could not parse time %r, using default %s", value, default)
        return default


def _get_config() -> dict:
    return {
        "cheap_start": _parse_hhmm(os.environ.get("CHEAP_START", "00:30"), time(0, 30)),
        "cheap_end": _parse_hhmm(os.environ.get("CHEAP_END", "04:30"), time(4, 30)),
        "charge_target": float(os.environ.get("CHARGE_TARGET", "90")),
        "min_soc": float(os.environ.get("MIN_SOC", "15")),
    }


def _in_cheap_window(now: time, start: time, end: time) -> bool:
    """Return True if ``now`` falls within the cheap tariff window.

    Handles windows that wrap midnight (e.g. 23:30 → 04:30).
    """
    if start <= end:
        return start <= now < end
    # Wraps midnight
    return now >= start or now < end


def decide_mode(
    bms: BmsSnapshot,
    pv: AuroraSnapshot,
    house_power_w: float,
    now: datetime | None = None,
) -> str:
    """Return the desired battery mode string.

    Parameters
    ----------
    bms:
        Current battery state.
    pv:
        Current PV inverter state.
    house_power_w:
        Current house consumption in watts (positive = consuming power from
        grid/battery/solar). When a real CT-clamp measurement is not
        available, the caller may pass the magnitude of battery discharge
        power as a proxy (see ``controller/main.py``).
    now:
        Current datetime (defaults to ``datetime.now()`` for easy testing).

    Returns
    -------
    str
        One of: ``"idle"``, ``"charge"``, ``"discharge"``, ``"force_charge"``,
        ``"force_discharge"``.
    """
    if now is None:
        now = datetime.now()

    cfg = _get_config()
    current_time = now.time()

    soc = bms.soc_pct
    pv_w = pv.ac_power_w  # what the solar inverter is actually injecting

    in_cheap = _in_cheap_window(current_time, cfg["cheap_start"], cfg["cheap_end"])

    # --- Safety floor: never let battery drop below min_soc ---
    if soc <= cfg["min_soc"]:
        logger.info("SoC %.1f%% at floor – force_charge", soc)
        return "force_charge"

    # --- Cheap tariff window: charge to target ---
    if in_cheap:
        if soc < cfg["charge_target"]:
            logger.info(
                "In cheap window, SoC %.1f%% < target %.1f%% – charge",
                soc,
                cfg["charge_target"],
            )
            return "charge"
        logger.info("In cheap window, SoC %.1f%% already at target – idle", soc)
        return "idle"

    # --- Daytime: solar available ---
    if pv.is_producing:
        # Enough solar to cover house demand without touching the battery
        if pv_w >= house_power_w:
            if soc < cfg["charge_target"]:
                # Excess solar — let the inverter charge the battery from PV
                logger.info(
                    "PV %.0fW ≥ house %.0fW, SoC %.1f%% – charge from PV",
                    pv_w,
                    house_power_w,
                    soc,
                )
                return "charge"
            logger.info(
                "PV %.0fW ≥ house %.0fW, battery full – idle",
                pv_w,
                house_power_w,
            )
            return "idle"
        # Solar not enough – supplement from battery
        logger.info(
            "PV %.0fW < house %.0fW – discharge (SoC %.1f%%)",
            pv_w,
            house_power_w,
            soc,
        )
        return "discharge"

    # --- Night / no PV: discharge battery to supply house ---
    if soc > cfg["min_soc"]:
        logger.info("No PV, SoC %.1f%% > floor – discharge", soc)
        return "discharge"

    logger.info("No PV, SoC %.1f%% at floor – idle", soc)
    return "idle"
