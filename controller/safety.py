"""Safety enforcement: ensure inverter commands respect the BMS limits.

The Pylontech BMS publishes charge and discharge voltage/current limits
that the inverter MUST NOT exceed. This module translates those limits
into concrete P18 commands and applies them before any charge or
discharge operation begins.

If the BMS is unreachable the controller will not permit charge or
discharge operations (fail-safe).
"""

from __future__ import annotations

import logging

from pylontech_driver.bms import BmsSnapshot
from p18_serial.p18 import P18Inverter

logger = logging.getLogger(__name__)

# Absolute hardware limits for the Iconica 5.5 kW / Pylontech US2000C stack
_MAX_CHARGE_AMPS = 100.0
_MAX_DISCHARGE_AMPS = 100.0

# Minimum SoC below which we never discharge (extra margin on top of BMS)
MIN_DISCHARGE_SOC_PCT = 15.0

# SoC at which we stop charging (battery protection)
MAX_CHARGE_SOC_PCT = 95.0


class SafetyError(Exception):
    """Raised when a requested operation would violate BMS limits."""


def apply_bms_limits(inverter: P18Inverter, bms: BmsSnapshot) -> None:
    """Read BMS charge/discharge current limits and program them into the inverter.

    This should be called once after every successful BMS poll before
    issuing any charge or discharge command.

    Parameters
    ----------
    inverter:
        Open P18Inverter instance.
    bms:
        Latest BmsSnapshot from the battery stack.

    Raises
    ------
    SafetyError
        If the BMS limits are missing (BMS unreachable).
    """
    if bms.charge_current_limit_a is None or bms.discharge_current_limit_a is None:
        raise SafetyError(
            "BMS charge/discharge current limits are unavailable — "
            "refusing to operate inverter."
        )

    charge_a = min(bms.charge_current_limit_a, _MAX_CHARGE_AMPS)
    discharge_a = min(bms.discharge_current_limit_a, _MAX_DISCHARGE_AMPS)

    logger.info(
        "Applying BMS limits: charge ≤ %.0fA, discharge ≤ %.0fA",
        charge_a,
        discharge_a,
    )
    inverter.set_max_charge_current(int(charge_a))
    inverter.set_ac_charge_current(int(charge_a))

    # Programme the battery voltage limits when available
    if bms.charge_voltage_limit_v is not None:
        inverter.set_battery_recharge_voltage(bms.charge_voltage_limit_v)
    if bms.discharge_voltage_limit_v is not None:
        inverter.set_battery_cutoff_voltage(bms.discharge_voltage_limit_v)


def check_charge_allowed(bms: BmsSnapshot) -> None:
    """Raise SafetyError if charging is not safe given the current BMS state.

    Parameters
    ----------
    bms:
        Latest BmsSnapshot.

    Raises
    ------
    SafetyError
        When SoC is at or above the charge ceiling.
    """
    if bms.soc_pct >= MAX_CHARGE_SOC_PCT:
        raise SafetyError(
            f"Battery SoC {bms.soc_pct:.1f}% ≥ {MAX_CHARGE_SOC_PCT}% — "
            "charge not needed."
        )


def check_discharge_allowed(bms: BmsSnapshot) -> None:
    """Raise SafetyError if discharging is not safe given the current BMS state.

    Parameters
    ----------
    bms:
        Latest BmsSnapshot.

    Raises
    ------
    SafetyError
        When SoC is at or below the discharge floor.
    """
    if bms.soc_pct <= MIN_DISCHARGE_SOC_PCT:
        raise SafetyError(
            f"Battery SoC {bms.soc_pct:.1f}% ≤ {MIN_DISCHARGE_SOC_PCT}% — "
            "discharge not permitted."
        )
