"""
Powervault P3 MQTT bridge with Home Assistant auto-discovery.

This script:
  1. Subscribes to the P3's built-in MQTT broker (read-only, no auth).
  2. Parses every incoming message using ``pv3_monitor.topics``.
  3. Publishes HA MQTT auto-discovery config messages so entities appear
     automatically in Home Assistant without any manual YAML.
  4. Publishes normalised sensor states to ``powervault/<device_id>/sensor/<name>/state``.
  5. Subscribes to HA control topics and forwards schedule commands to the M4
     via SSH (when M4 SSH credentials are configured).

Environment variables (see .env.example):
  P3_HOST, P3_MQTT_PORT, P3_DEVICE_ID
  HA_MQTT_HOST, HA_MQTT_PORT, HA_MQTT_USER, HA_MQTT_PASS
  M4_SSH_HOST, M4_SSH_PORT, M4_SSH_USER, M4_SSH_KEY_FILE
  LOG_LEVEL
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt

from .ha_discovery import (
    SCHEDULE_OPTIONS,
    control_topic_for,
    schedule_select_config,
    sensor_config,
    setpoint_number_config,
    state_topic_for,
)
from .schedule_control import (
    CHARGE,
    DISCHARGE,
    FORCE_CHARGE,
    FORCE_DISCHARGE,
    IDLE,
    ScheduleController,
)
from .topics import SensorReading, parse_topic

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

P3_HOST = os.getenv("P3_HOST", "192.168.1.215")
P3_MQTT_PORT = int(os.getenv("P3_MQTT_PORT", "1883"))
P3_DEVICE_ID = os.getenv("P3_DEVICE_ID", "PV001001DEV")

HA_MQTT_HOST = os.getenv("HA_MQTT_HOST", "")
HA_MQTT_PORT = int(os.getenv("HA_MQTT_PORT", "1883"))
HA_MQTT_USER = os.getenv("HA_MQTT_USER", "")
HA_MQTT_PASS = os.getenv("HA_MQTT_PASS", "")

M4_SSH_HOST = os.getenv("M4_SSH_HOST", "")
M4_SSH_PORT = int(os.getenv("M4_SSH_PORT", "22"))
M4_SSH_USER = os.getenv("M4_SSH_USER", "root")
M4_SSH_KEY_FILE = os.getenv("M4_SSH_KEY_FILE", "") or None
M4_KNOWN_HOSTS_FILE = os.getenv("M4_KNOWN_HOSTS_FILE", "") or None

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pv3_monitor.bridge")

# Map from HA schedule option string → event code
_OPTION_TO_EVENT = {name: code for code, name in enumerate(SCHEDULE_OPTIONS)}


# ---------------------------------------------------------------------------
# Bridge class
# ---------------------------------------------------------------------------

class PV3Bridge:
    """MQTT bridge between the P3 broker and the HA broker."""

    def __init__(self) -> None:
        self._discovery_published: set[str] = set()
        self._schedule_ctrl: ScheduleController | None = None

        if M4_SSH_HOST:
            self._schedule_ctrl = ScheduleController(
                host=M4_SSH_HOST,
                port=M4_SSH_PORT,
                username=M4_SSH_USER,
                key_file=M4_SSH_KEY_FILE,
                known_hosts_file=M4_KNOWN_HOSTS_FILE,
            )
            logger.info("Schedule control enabled (SSH → %s)", M4_SSH_HOST)
        else:
            logger.warning(
                "M4_SSH_HOST not set — schedule control is disabled. "
                "Configure SSH credentials in .env to enable control."
            )

        # P3 subscriber client
        self._p3_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="pv3_bridge_p3",
        )
        self._p3_client.on_connect = self._on_p3_connect
        self._p3_client.on_message = self._on_p3_message
        self._p3_client.on_disconnect = self._on_p3_disconnect

        # HA publisher client (may be same broker as P3 when HA_MQTT_HOST == P3_HOST)
        self._ha_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="pv3_bridge_ha",
        )
        if HA_MQTT_USER:
            self._ha_client.username_pw_set(HA_MQTT_USER, HA_MQTT_PASS)
        self._ha_client.on_connect = self._on_ha_connect
        self._ha_client.on_message = self._on_ha_message
        self._ha_client.on_disconnect = self._on_ha_disconnect

    # ------------------------------------------------------------------
    # P3 MQTT callbacks
    # ------------------------------------------------------------------

    def _on_p3_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        rc: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if rc.is_failure:
            logger.error("P3 broker connect failed: %s", rc)
            return
        topic = f"pv/PV3/{P3_DEVICE_ID}/#"
        client.subscribe(topic, qos=0)
        logger.info("Connected to P3 broker at %s:%d — subscribed to %s", P3_HOST, P3_MQTT_PORT, topic)

    def _on_p3_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        rc: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        logger.warning("Disconnected from P3 broker: %s", rc)

    def _on_p3_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        try:
            parts = msg.topic.split("/")
            # Expected: pv / PV3 / <DEVICE_ID> / <suffix…>
            if len(parts) < 4:
                return
            topic_suffix = "/".join(parts[3:])

            try:
                payload = json.loads(msg.payload.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.debug("Non-JSON payload on %s — ignored", msg.topic)
                return

            readings = parse_topic(topic_suffix, payload)
            for reading in readings:
                self._publish_sensor(reading)

        except Exception:
            logger.exception("Error processing P3 message from %s", msg.topic)

    # ------------------------------------------------------------------
    # HA MQTT callbacks
    # ------------------------------------------------------------------

    def _on_ha_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        rc: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if rc.is_failure:
            logger.error("HA broker connect failed: %s", rc)
            return

        ha_host = HA_MQTT_HOST or P3_HOST
        logger.info("Connected to HA broker at %s:%d", ha_host, HA_MQTT_PORT)

        # Subscribe to control topics
        mode_cmd = control_topic_for(P3_DEVICE_ID, "schedule/mode")
        setpoint_cmd = control_topic_for(P3_DEVICE_ID, "schedule/setpoint")
        client.subscribe([(mode_cmd, 1), (setpoint_cmd, 1)])
        logger.info("Subscribed to control topics: %s, %s", mode_cmd, setpoint_cmd)

        # Publish auto-discovery for control entities
        self._publish_control_discovery()

    def _on_ha_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        rc: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        logger.warning("Disconnected from HA broker: %s", rc)

    def _on_ha_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Handle control commands from Home Assistant."""
        try:
            payload_str = msg.payload.decode().strip()
            mode_cmd = control_topic_for(P3_DEVICE_ID, "schedule/mode")
            setpoint_cmd = control_topic_for(P3_DEVICE_ID, "schedule/setpoint")

            if msg.topic == mode_cmd:
                self._handle_mode_command(payload_str)
            elif msg.topic == setpoint_cmd:
                self._handle_setpoint_command(payload_str)

        except Exception:
            logger.exception("Error processing HA control message from %s", msg.topic)

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------

    def _handle_mode_command(self, option: str) -> None:
        """Map an HA select option string to a schedule event and apply it."""
        if not self._schedule_ctrl:
            logger.warning(
                "Received mode command '%s' but schedule control is not configured. "
                "Set M4_SSH_HOST and SSH credentials in .env.",
                option,
            )
            return

        event = _OPTION_TO_EVENT.get(option)
        if event is None:
            logger.error("Unknown schedule option: %s", option)
            return

        logger.info("Applying schedule mode: %s (event %d)", option, event)
        try:
            self._schedule_ctrl.set_mode(event)
            # Echo the new state back to HA
            state_topic = state_topic_for(P3_DEVICE_ID, "schedule_mode")
            self._ha_client.publish(state_topic, option, qos=1, retain=True)
        except Exception:
            logger.exception("Failed to apply schedule mode %s", option)

    def _handle_setpoint_command(self, value_str: str) -> None:
        """Handle a setpoint change (watts) — stored for next mode command."""
        try:
            watts = int(float(value_str))
            logger.info("Schedule setpoint updated to %d W", watts)
            # Echo back to HA
            state_topic = state_topic_for(P3_DEVICE_ID, "schedule_setpoint_control")
            self._ha_client.publish(state_topic, str(watts), qos=1, retain=True)
        except ValueError:
            logger.error("Invalid setpoint value: %s", value_str)

    # ------------------------------------------------------------------
    # Publishing helpers
    # ------------------------------------------------------------------

    def _publish_sensor(self, reading: SensorReading) -> None:
        """Publish HA discovery (once) and current state for *reading*."""
        s_topic = state_topic_for(P3_DEVICE_ID, reading.name)

        # Auto-discovery (publish once per unique name)
        if reading.name not in self._discovery_published:
            d_topic, d_payload = sensor_config(
                reading=reading,
                device_id=P3_DEVICE_ID,
                state_topic=s_topic,
            )
            self._ha_client.publish(d_topic, d_payload, qos=1, retain=True)
            self._discovery_published.add(reading.name)
            logger.debug("Published discovery for %s", reading.name)

        # Current state
        self._ha_client.publish(s_topic, str(reading.value), qos=0, retain=False)

    def _publish_control_discovery(self) -> None:
        """Publish auto-discovery payloads for the schedule control entities."""
        mode_cmd = control_topic_for(P3_DEVICE_ID, "schedule/mode")
        mode_state = state_topic_for(P3_DEVICE_ID, "schedule_mode")
        setpoint_cmd = control_topic_for(P3_DEVICE_ID, "schedule/setpoint")
        setpoint_state = state_topic_for(P3_DEVICE_ID, "schedule_setpoint_control")

        select_topic, select_payload = schedule_select_config(
            device_id=P3_DEVICE_ID,
            command_topic=mode_cmd,
            state_topic=mode_state,
        )
        self._ha_client.publish(select_topic, select_payload, qos=1, retain=True)

        number_topic, number_payload = setpoint_number_config(
            device_id=P3_DEVICE_ID,
            command_topic=setpoint_cmd,
            state_topic=setpoint_state,
        )
        self._ha_client.publish(number_topic, number_payload, qos=1, retain=True)

        logger.info("Published control entity discovery (schedule select + setpoint number)")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect both clients and run the bridge loop forever."""
        ha_host = HA_MQTT_HOST or P3_HOST
        logger.info("Starting PV3 MQTT bridge")
        logger.info("  P3 broker:  %s:%d  device_id=%s", P3_HOST, P3_MQTT_PORT, P3_DEVICE_ID)
        logger.info("  HA broker:  %s:%d", ha_host, HA_MQTT_PORT)

        # Connect HA client first so it is ready before messages flow
        self._connect_ha(ha_host)
        self._connect_p3()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down bridge…")
        finally:
            self._p3_client.disconnect()
            self._ha_client.disconnect()

    def _connect_p3(self) -> None:
        self._p3_client.connect_async(P3_HOST, P3_MQTT_PORT, keepalive=60)
        self._p3_client.loop_start()

    def _connect_ha(self, host: str) -> None:
        self._ha_client.connect_async(host, HA_MQTT_PORT, keepalive=60)
        self._ha_client.loop_start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Load .env (if present) and start the bridge."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    bridge = PV3Bridge()
    bridge.run()


if __name__ == "__main__":
    main()
