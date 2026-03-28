"""Standalone M4-bypass controller for the Powervault P3.

This is the entry point for the fully independent local control stack.
It replaces the dependency on the M4 controller's MQTT broker by talking
directly to the hardware:

  Pylontech batteries  ← RS485    → /dev/ttyUSB1 (python-pylontech)
  Iconica inverter     ← USB/RS232 → /dev/ttyUSB0 (p18_serial)
  ABB UNO PVI 3.0      ← RS485    → /dev/ttyUSB2 (aurorapy) [optional]

Control model
-------------
The charge mode is set externally via a Home Assistant MQTT ``select``
entity.  HA publishes one of the five mode strings to the command topic:

  powervault/{device_id}/control/charge_mode/set

The controller subscribes to this topic, updates its in-memory mode, and
on every poll cycle applies that mode to the inverter (with BMS safety
checks).  The current mode is reflected back on the state topic:

  powervault/{device_id}/control/charge_mode/state

On startup the controller publishes HA MQTT auto-discovery configs for all
sensors (BMS, PV inverter) and the charge-mode select entity, so entities
appear in HA automatically without any manual YAML.

Charge modes
------------
  idle          – hold battery (neither charge nor discharge)
  charge        – charge from grid/solar (soft ceiling check)
  discharge     – discharge to supply load (soft floor check)
  force_charge  – maximum grid charge (bypasses ceiling)
  force_discharge – maximum discharge (bypasses ceiling, floor applies)

BMS safety checks always apply and will revert to idle if violated.

Shelly EM grid-power integration
---------------------------------
Set ``SHELLY_GRID_POWER_TOPIC`` to the MQTT topic your Shelly EM device
publishes grid power on (e.g. ``shellies/shellyem-aabbcc/emeter/0/power``
for Gen1, or the equivalent Gen2/Gen3 JSON topic).  The controller will:

  1. Subscribe to that topic and parse the power value (plain float **or**
     JSON with ``power`` / ``apower`` / ``act_power`` keys).
  2. Publish the reading as an HA sensor (``grid_power``).
  3. Wake the control loop immediately so the inverter mode is re-evaluated
     within ``PEAK_SHAVE_MIN_APPLY_INTERVAL`` seconds (default 2 s) of the
     reading arriving — far faster than the normal ``POLL_INTERVAL``.

If you have CT clamps on **two** fuse boards, set ``SHELLY_GRID_POWER_TOPIC_2``
to the second emeter channel topic (e.g.
``shellies/shellyem-aabbcc/emeter/1/power``).  When both topics are
configured the controller sums the two readings so that ``grid_power``
represents the total import/export across both boards.

Note: the Shelly CT clamp plugs into the Shelly EM hardware device, which
connects to your WiFi network.  The CT clamp cannot be wired directly to
the Raspberry Pi GPIO pins.

Peak-shaving
------------
Set ``PEAK_SHAVE_ENABLED=true`` together with ``SHELLY_GRID_POWER_TOPIC``
to enable automatic peak-shaving.  When grid import exceeds
``PEAK_SHAVE_IMPORT_W`` (+ ``PEAK_SHAVE_HYSTERESIS_W`` deadband), the
controller automatically overrides the HA-selected mode with ``discharge``
so the battery covers the spike without pulling from the grid.

Run directly:
  python -m controller.main

Or via docker-compose with the ``controller`` profile.

Environment variables
---------------------
  INVERTER_PORT         Serial/USB port for the Iconica inverter
  BATTERY_PORT          RS485 port for the Pylontech batteries
  NUM_BATTERY_MODULES   Number of Pylontech modules in the stack

  HA_MQTT_HOST          Home Assistant MQTT broker hostname
  HA_MQTT_PORT          HA broker port (default 1883)
  HA_MQTT_USER          HA broker username (optional)
  HA_MQTT_PASS          HA broker password (optional)

  PV_INVERTER_PORT      RS485 port for the ABB Aurora PV inverter
                        (leave unset to disable PV inverter polling)
  PV_INVERTER_ADDRESS   Aurora RS485 address (default 2)
  DEVICE_ID             Identifier used in MQTT topics (default "powervault")
  POLL_INTERVAL         Seconds between full BMS/PV polling cycles (default 30)
  INVERTER_IS_USB       Set to "true" if using USB HID port (default false)

  SHELLY_GRID_POWER_TOPIC   MQTT topic for Shelly EM channel 1 grid power readings
                             (leave unset to disable Shelly integration)
  SHELLY_GRID_POWER_TOPIC_2 MQTT topic for Shelly EM channel 2 grid power readings
                             (optional; when set, grid power = ch1 + ch2)
  PEAK_SHAVE_ENABLED         Set to "true" to enable automatic peak-shaving
  PEAK_SHAVE_IMPORT_W        Grid import threshold in W above which discharge
                             is triggered automatically (default 0)
  PEAK_SHAVE_HYSTERESIS_W    Deadband in W to prevent rapid mode toggling
                             (default 50)
  PEAK_SHAVE_MIN_APPLY_INTERVAL  Minimum seconds between fast-path inverter
                             commands (default 2)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from pylontech_driver.bms import BmsPoller, BmsSnapshot
from abb_aurora.aurora import AbbAuroraPoller, AuroraSnapshot
from p18_serial.p18 import P18Inverter
from controller.safety import (
    SafetyError,
    apply_bms_limits,
    check_charge_allowed,
    check_discharge_allowed,
)
from controller import ha_discovery
from controller.ha_discovery import CHARGE_MODES


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Shelly EM helpers
# ---------------------------------------------------------------------------

def _parse_shelly_power(payload: bytes) -> float | None:
    """Parse a Shelly EM MQTT power payload.

    Supports:

    - **Shelly Gen1** (plain float string): ``"1234.5"``
    - **Shelly Gen2 / Gen3** (JSON with various key names):
      ``{"apower": 1234.5, ...}`` or ``{"act_power": 1234.5, ...}``

    Returns the active power in watts (positive = import, negative = export),
    or *None* if the payload cannot be parsed.
    """
    text = payload.decode(errors="replace").strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        data = json.loads(text)
        for key in ("power", "apower", "act_power", "a_act_power", "total_act_power"):
            if key in data and data[key] is not None:
                return float(data[key])
    except (ValueError, KeyError, TypeError):
        pass
    return None


def _compute_grid_total(readings: list[float | None]) -> float | None:
    """Sum all non-None per-channel power readings.

    Returns *None* if no channel has received a reading yet, so the caller
    can distinguish "no data" from "zero watts".
    """
    values = [v for v in readings if v is not None]
    return sum(values) if values else None


def _effective_mode(
    requested: str,
    grid_w: float | None,
    peak_shave: bool,
    import_threshold_w: float,
    hysteresis_w: float,
) -> str:
    """Return the effective inverter mode, applying peak-shaving if active.

    When peak-shaving is enabled and the grid import exceeds
    *import_threshold_w* + *hysteresis_w*, the mode is overridden to
    ``"discharge"`` regardless of the HA-requested mode.  The hysteresis
    prevents rapid toggling near the threshold.

    Parameters
    ----------
    requested:
        The charge mode last requested by HA (or ``"idle"`` by default).
    grid_w:
        Latest grid power in watts (positive = import, negative = export),
        or *None* if no Shelly reading has been received yet.
    peak_shave:
        Whether peak-shaving auto-discharge is enabled.
    import_threshold_w:
        Grid import level in watts above which battery discharge is triggered.
    hysteresis_w:
        Additional deadband above *import_threshold_w* to avoid rapid
        mode switching.
    """
    if (
        peak_shave
        and grid_w is not None
        and grid_w > import_threshold_w + hysteresis_w
    ):
        return "discharge"
    return requested


# ---------------------------------------------------------------------------
# MQTT client factory
# ---------------------------------------------------------------------------

def _build_mqtt_client(
    device_id: str,
    on_mode_command: object,
    logger: logging.Logger,
    shelly_topic: str = "",
    on_shelly_power: object = None,
    shelly_topic_2: str = "",
    on_shelly_power_2: object = None,
) -> mqtt.Client:
    """Build, configure and connect the MQTT client.

    Sets up:
    - Last Will and Testament (publishes "offline" on unexpected disconnect)
    - ``on_connect`` handler that resubscribes to the mode command topic and,
      if configured, the Shelly grid-power topic(s)
    - ``on_message`` handler that routes mode-command and Shelly messages
    """
    cmd_topic = ha_discovery.charge_mode_command_topic(device_id)
    avail_topic = ha_discovery.availability_topic(device_id)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    # LWT: HA will mark all entities unavailable if we disconnect unexpectedly
    client.will_set(avail_topic, "offline", retain=True)

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object = None,
    ) -> None:
        if str(reason_code) == "Success":
            client.subscribe(cmd_topic)
            logger.info("MQTT connected — subscribed to %s", cmd_topic)
            if shelly_topic:
                client.subscribe(shelly_topic)
                logger.info("Subscribed to Shelly topic 1: %s", shelly_topic)
            if shelly_topic_2:
                client.subscribe(shelly_topic_2)
                logger.info("Subscribed to Shelly topic 2: %s", shelly_topic_2)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def on_message(
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        if message.topic == cmd_topic:
            on_mode_command(client, message)
        elif shelly_topic and message.topic == shelly_topic and on_shelly_power:
            on_shelly_power(message)
        elif shelly_topic_2 and message.topic == shelly_topic_2 and on_shelly_power_2:
            on_shelly_power_2(message)

    client.on_connect = on_connect
    client.on_message = on_message

    user = _env("HA_MQTT_USER")
    password = _env("HA_MQTT_PASS")
    if user:
        client.username_pw_set(user, password or None)

    client.connect(
        _env("HA_MQTT_HOST", "localhost"),
        _env_int("HA_MQTT_PORT", 1883),
    )
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# MQTT publishers
# ---------------------------------------------------------------------------

def _sensor_topic(device_id: str, name: str) -> str:
    return ha_discovery.sensor_state_topic(device_id, name)


def _publish_bms(client: mqtt.Client, device_id: str, snap: BmsSnapshot) -> None:
    """Publish all BMS sensor readings to HA MQTT."""
    readings: dict[str, object] = {
        "battery_soc": round(snap.soc_pct, 1),
        "battery_soh": round(snap.soh_pct, 1),
        "battery_soh_min": round(snap.soh_pct_min, 1),
        "battery_cycle_count": int(snap.cycle_count_max),
        "battery_voltage": round(snap.battery_voltage_v, 3),
        "battery_current": round(snap.battery_current_a, 3),
        "battery_power": round(snap.battery_power_w, 1),
        "battery_cell_voltage_max": round(snap.cell_voltage_max_v, 4),
        "battery_cell_voltage_min": round(snap.cell_voltage_min_v, 4),
        "battery_cell_temp_avg": round(snap.cell_temp_avg_c, 1),
        "battery_cell_temp_max": round(snap.cell_temp_max_c, 1),
        "battery_cell_temp_min": round(snap.cell_temp_min_c, 1),
    }
    if snap.charge_current_limit_a is not None:
        readings["bms_charge_current_limit"] = round(snap.charge_current_limit_a, 1)
    if snap.discharge_current_limit_a is not None:
        readings["bms_discharge_current_limit"] = round(snap.discharge_current_limit_a, 1)
    if snap.charge_voltage_limit_v is not None:
        readings["bms_charge_voltage_limit"] = round(snap.charge_voltage_limit_v, 2)
    if snap.discharge_voltage_limit_v is not None:
        readings["bms_discharge_voltage_limit"] = round(snap.discharge_voltage_limit_v, 2)

    for name, value in readings.items():
        client.publish(_sensor_topic(device_id, name), str(value), retain=True)


def _publish_pv(client: mqtt.Client, device_id: str, snap: AuroraSnapshot) -> None:
    """Publish ABB PV inverter readings to HA MQTT."""
    readings: dict[str, object] = {
        "pv_ac_power": round(snap.ac_power_w, 1),
        "pv_dc_power": round(snap.dc_power_w, 1),
        "pv_producing": "ON" if snap.is_producing else "OFF",
    }
    if snap.dc_voltage_v is not None:
        readings["pv_dc_voltage"] = round(snap.dc_voltage_v, 2)
    if snap.dc_current_a is not None:
        readings["pv_dc_current"] = round(snap.dc_current_a, 3)
    if snap.grid_voltage_v is not None:
        readings["pv_grid_voltage"] = round(snap.grid_voltage_v, 2)
    if snap.grid_frequency_hz is not None:
        readings["pv_grid_frequency"] = round(snap.grid_frequency_hz, 2)
    if snap.temperature_c is not None:
        readings["pv_temperature"] = round(snap.temperature_c, 1)
    if snap.energy_today_kwh is not None:
        readings["pv_energy_today"] = round(snap.energy_today_kwh, 3)
    if snap.energy_total_kwh is not None:
        readings["pv_energy_total"] = round(snap.energy_total_kwh, 3)
    if snap.inverter_state is not None:
        readings["pv_state"] = snap.inverter_state

    for name, value in readings.items():
        client.publish(_sensor_topic(device_id, name), str(value), retain=True)


# ---------------------------------------------------------------------------
# Inverter mode application (with BMS safety)
# ---------------------------------------------------------------------------

def _apply_mode(
    inverter: P18Inverter,
    bms: BmsSnapshot,
    mode: str,
    logger: logging.Logger,
) -> None:
    """Apply *mode* to the inverter, respecting BMS limits and safety checks.

    Falls back to idle if the BMS limits are unavailable or a safety check
    blocks the requested mode.
    """
    try:
        apply_bms_limits(inverter, bms)
    except SafetyError as exc:
        logger.error("BMS limit application failed: %s — going idle", exc)
        inverter.idle_mode()
        return

    try:
        if mode == "charge":
            check_charge_allowed(bms)
            inverter.force_charge()
        elif mode == "discharge":
            check_discharge_allowed(bms)
            inverter.normal_mode()
        elif mode == "force_charge":
            inverter.force_charge()
        elif mode == "force_discharge":
            check_discharge_allowed(bms)
            inverter.normal_mode()
        else:  # idle (default / unknown)
            inverter.idle_mode()
        logger.info("Inverter mode applied: %s", mode)
    except SafetyError as exc:
        logger.warning("Mode %r blocked by safety: %s — going idle", mode, exc)
        inverter.idle_mode()
    except Exception as exc:
        logger.error("Failed to apply mode %r to inverter: %s", mode, exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Main polling and control loop — runs indefinitely."""
    load_dotenv()

    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger(__name__)

    device_id = _env("DEVICE_ID", "powervault")
    poll_interval = _env_int("POLL_INTERVAL", 30)

    inverter_port = _env("INVERTER_PORT", "/dev/ttyUSB0")
    is_usb = _env_bool("INVERTER_IS_USB", False)

    battery_port = _env("BATTERY_PORT", "/dev/ttyUSB1")
    num_modules = _env_int("NUM_BATTERY_MODULES", 1)

    pv_port = _env("PV_INVERTER_PORT", "")
    pv_address = _env_int("PV_INVERTER_ADDRESS", 2)

    # Shelly EM / peak-shaving configuration
    shelly_topic = _env("SHELLY_GRID_POWER_TOPIC", "")
    shelly_topic_2 = _env("SHELLY_GRID_POWER_TOPIC_2", "")
    peak_shave_enabled = _env_bool("PEAK_SHAVE_ENABLED", False)
    peak_shave_import_w = _env_float("PEAK_SHAVE_IMPORT_W", 0.0)
    peak_shave_hysteresis_w = _env_float("PEAK_SHAVE_HYSTERESIS_W", 50.0)
    # Minimum seconds between fast-path inverter commands (debounce)
    min_apply_interval = _env_float("PEAK_SHAVE_MIN_APPLY_INTERVAL", 2.0)

    logger.info("Starting M4-bypass controller (device_id=%s)", device_id)
    logger.info("  Inverter:    %s (usb=%s)", inverter_port, is_usb)
    logger.info("  Batteries:   %s (%d modules)", battery_port, num_modules)
    if pv_port:
        logger.info("  PV inverter: %s (address %d)", pv_port, pv_address)
    else:
        logger.info("  PV inverter: disabled (set PV_INVERTER_PORT to enable)")
    if shelly_topic and shelly_topic_2:
        logger.info(
            "  Shelly grid: ch1=%s ch2=%s (summed, peak_shave=%s)",
            shelly_topic, shelly_topic_2, peak_shave_enabled,
        )
    elif shelly_topic:
        logger.info("  Shelly grid: %s (peak_shave=%s)", shelly_topic, peak_shave_enabled)
    else:
        logger.info("  Shelly grid: disabled (set SHELLY_GRID_POWER_TOPIC to enable)")

    # --- Thread-safe mode storage (written by MQTT callback, read by main loop) ---
    # One-element lists are used so closures can mutate them.
    _mode: list[str] = ["idle"]
    _latest_grid_w: list[float | None] = [None]
    # Per-channel Shelly readings; index 0 = topic 1, index 1 = topic 2.
    _shelly_channels: list[float | None] = [None, None]
    # Timestamp of the last inverter command (monotonic); used for fast-path debounce.
    _last_apply_time: list[float] = [0.0]
    # Event set by the Shelly MQTT callback to wake the main loop early.
    _fast_apply = threading.Event()
    # Forward reference so Shelly handlers can publish via the MQTT client.
    _client_ref: list[mqtt.Client | None] = [None]

    def on_mode_command(client: mqtt.Client, message: mqtt.MQTTMessage) -> None:
        mode = message.payload.decode().strip().lower()
        if mode in CHARGE_MODES:
            _mode[0] = mode
            logger.info("Charge mode command received: %s", mode)
            # Immediately reflect the new state back to HA
            client.publish(
                ha_discovery.charge_mode_state_topic(device_id),
                mode,
                retain=True,
            )
        else:
            logger.warning("Unknown charge mode received: %r (ignored)", mode)

    def _make_shelly_handler(channel_idx: int):
        """Return an MQTT message handler for the given Shelly channel index."""
        def handler(message: mqtt.MQTTMessage) -> None:
            power = _parse_shelly_power(message.payload)
            if power is not None:
                _shelly_channels[channel_idx] = power
                total = _compute_grid_total(_shelly_channels)
                _latest_grid_w[0] = total
                if _client_ref[0] is not None and total is not None:
                    _client_ref[0].publish(
                        _sensor_topic(device_id, "grid_power"),
                        str(round(total, 1)),
                        retain=False,
                    )
                logger.debug(
                    "Shelly ch%d: %.1f W (ch1=%s, ch2=%s, total=%.1f W)",
                    channel_idx + 1,
                    power,
                    f"{_shelly_channels[0]:.1f}" if _shelly_channels[0] is not None else "—",
                    f"{_shelly_channels[1]:.1f}" if _shelly_channels[1] is not None else "—",
                    total if total is not None else 0.0,
                )
                _fast_apply.set()
            else:
                logger.warning(
                    "Could not parse Shelly payload (ch%d): %r",
                    channel_idx + 1,
                    message.payload[:64],
                )
        return handler

    on_shelly_ch1 = _make_shelly_handler(0) if shelly_topic else None
    on_shelly_ch2 = _make_shelly_handler(1) if shelly_topic_2 else None

    mqtt_client = _build_mqtt_client(
        device_id,
        on_mode_command,
        logger,
        shelly_topic=shelly_topic,
        on_shelly_power=on_shelly_ch1,
        shelly_topic_2=shelly_topic_2,
        on_shelly_power_2=on_shelly_ch2,
    )
    _client_ref[0] = mqtt_client
    avail_topic = ha_discovery.availability_topic(device_id)

    # Publish all HA auto-discovery configs (retained, so HA picks them up)
    configs = ha_discovery.all_discovery_configs(device_id)
    for topic, payload in configs:
        mqtt_client.publish(topic, payload, retain=True)
    logger.info("Published %d HA discovery configs", len(configs))

    # Publish Shelly grid-power sensor discovery only when configured
    if shelly_topic:
        shelly_disc = ha_discovery.shelly_grid_power_discovery(device_id)
        mqtt_client.publish(shelly_disc[0], shelly_disc[1], retain=True)
        logger.info("Published Shelly grid power discovery config")

    # Announce online + publish initial mode state
    mqtt_client.publish(avail_topic, "online", retain=True)
    mqtt_client.publish(
        ha_discovery.charge_mode_state_topic(device_id),
        _mode[0],
        retain=True,
    )

    bms_poller = BmsPoller(port=battery_port, num_modules=num_modules)
    pv_poller = AbbAuroraPoller(port=pv_port, address=pv_address) if pv_port else None

    try:
        with P18Inverter(port=inverter_port, is_usb=is_usb) as inverter:
            logger.info("Inverter connected")

            # Cache the last successful BMS snapshot so the fast path can use
            # it between full poll cycles without hitting the RS485 bus.
            cached_bms: BmsSnapshot | None = None

            while True:
                loop_start = time.monotonic()

                # 1. Read battery BMS
                bms: BmsSnapshot | None = None
                try:
                    bms = bms_poller.read()
                    cached_bms = bms
                    _publish_bms(mqtt_client, device_id, bms)
                    logger.info(
                        "BMS: SoC=%.1f%% V=%.2fV I=%.1fA",
                        bms.soc_pct,
                        bms.battery_voltage_v,
                        bms.battery_current_a,
                    )
                except Exception as exc:
                    logger.error("BMS poll failed: %s", exc)

                # 2. Read PV inverter (optional)
                if pv_poller is not None:
                    try:
                        pv = pv_poller.read()
                        _publish_pv(mqtt_client, device_id, pv)
                    except Exception as exc:
                        logger.error("PV inverter poll failed: %s", exc)

                # 3. Apply the effective mode (HA request + peak-shaving override)
                effective = _effective_mode(
                    _mode[0],
                    _latest_grid_w[0],
                    peak_shave_enabled,
                    peak_shave_import_w,
                    peak_shave_hysteresis_w,
                )
                if cached_bms is not None:
                    _apply_mode(inverter, cached_bms, effective, logger)
                    _last_apply_time[0] = time.monotonic()
                    # Re-publish state so HA stays in sync
                    mqtt_client.publish(
                        ha_discovery.charge_mode_state_topic(device_id),
                        effective,
                        retain=True,
                    )
                else:
                    logger.warning("No BMS data — skipping inverter control this cycle")

                # 4. Sleep remainder of the poll interval, but wake early when a
                #    Shelly grid-power reading arrives so we can respond quickly
                #    to sudden load spikes without waiting for the next full cycle.
                elapsed = time.monotonic() - loop_start
                remaining = max(0.0, poll_interval - elapsed)
                _fast_apply.clear()
                woken_early = _fast_apply.wait(timeout=remaining)

                if woken_early and cached_bms is not None:
                    now = time.monotonic()
                    if now - _last_apply_time[0] >= min_apply_interval:
                        fast_effective = _effective_mode(
                            _mode[0],
                            _latest_grid_w[0],
                            peak_shave_enabled,
                            peak_shave_import_w,
                            peak_shave_hysteresis_w,
                        )
                        logger.debug(
                            "Fast-path apply: grid=%.1f W mode=%s",
                            _latest_grid_w[0] or 0.0,
                            fast_effective,
                        )
                        _apply_mode(inverter, cached_bms, fast_effective, logger)
                        _last_apply_time[0] = now
                        mqtt_client.publish(
                            ha_discovery.charge_mode_state_topic(device_id),
                            fast_effective,
                            retain=True,
                        )

    except KeyboardInterrupt:
        logger.info("Controller stopped by user")
    finally:
        # Mark all entities unavailable in HA before exiting
        mqtt_client.publish(avail_topic, "offline", retain=True)
        time.sleep(0.5)  # allow last message to flush before disconnect
        bms_poller.close()
        if pv_poller is not None:
            pv_poller.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    run()

