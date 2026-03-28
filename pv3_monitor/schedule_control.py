"""
SSH-based schedule control for the Powervault P3 M4 controller.

The M4 controller runs an embedded Linux system that reads charge/discharge
schedules from a JSON file. By writing this file over SSH we can control
the battery operating mode without needing cloud connectivity.

**Status (as of December 2024/2025):**
  - The schedule file path is confirmed from Powervault cloud logs:
      /data/appconfigs/cloudconnection/state_schedules/ffr_schedule.json
  - The JSON format below is inferred from MQTT ``schedule/event`` messages;
    it should be verified against the actual file on the M4 filesystem once
    SSH access is established.
  - SSH access to the M4 has not yet been confirmed. Serial console access
    (DB9 RS-232 port, 115200 8N1) is the recommended next step.

**Schedule event codes (confirmed via MQTT):**
  0 = Idle
  1 = Charge
  2 = Discharge
  3 = Force Charge
  4 = Force Discharge
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Schedule event code constants
IDLE = 0
CHARGE = 1
DISCHARGE = 2
FORCE_CHARGE = 3
FORCE_DISCHARGE = 4

SCHEDULE_FILE = "/data/appconfigs/cloudconnection/state_schedules/ffr_schedule.json"


class ScheduleController:
    """Write schedule commands to the M4 controller over SSH.

    Usage::

        ctrl = ScheduleController(host="192.168.1.215", username="root")
        ctrl.set_mode(FORCE_CHARGE, setpoint_watts=3000)

    Parameters
    ----------
    host:
        IP address or hostname of the M4 controller.
    username:
        SSH username (typically ``root``).
    key_file:
        Path to an SSH private key file. If *None* the SSH agent or
        ``~/.ssh/id_*`` keys are tried.
    port:
        SSH port (default 22).
    """

    def __init__(
        self,
        host: str,
        username: str = "root",
        key_file: str | None = None,
        port: int = 22,
        known_hosts_file: str | None = None,
    ) -> None:
        self.host = host
        self.username = username
        self.key_file = key_file
        self.port = port
        self.known_hosts_file = known_hosts_file

    def _connect(self):
        """Return an open ``paramiko.SSHClient``."""
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "paramiko is required for SSH schedule control. "
                "Install it with: pip install paramiko"
            ) from exc

        client = paramiko.SSHClient()
        # Load known hosts from the specified file or the system known_hosts.
        # RejectPolicy means an unknown host key raises an exception rather
        # than being silently accepted.  To add the M4's host key run:
        #   ssh-keyscan -H <M4_IP> >> ~/.ssh/known_hosts
        if self.known_hosts_file:
            client.load_host_keys(self.known_hosts_file)
        else:
            client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        connect_kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": 10,
        }
        if self.key_file:
            connect_kwargs["key_filename"] = self.key_file

        client.connect(**connect_kwargs)
        return client

    def _write_schedule_file(self, schedule: dict) -> None:
        """Write *schedule* as JSON to the M4 schedule file over SSH."""
        payload = json.dumps(schedule)
        # Use a safe write-then-move pattern to avoid partial writes
        tmp = SCHEDULE_FILE + ".tmp"
        cmd = f"echo '{payload}' > {tmp} && mv {tmp} {SCHEDULE_FILE}"

        logger.info("Writing schedule to M4: %s", payload)
        client = self._connect()
        try:
            _stdin, stdout, stderr = client.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode().strip()
                raise RuntimeError(
                    f"SSH command failed (exit {exit_code}): {err}"
                )
        finally:
            client.close()

        logger.info("Schedule written successfully.")

    def set_mode(self, event: int, setpoint_watts: int = 0) -> None:
        """Set the battery operating mode.

        Parameters
        ----------
        event:
            One of the module-level constants: IDLE, CHARGE, DISCHARGE,
            FORCE_CHARGE, FORCE_DISCHARGE.
        setpoint_watts:
            Target power in watts (positive). Not all modes use this.
        """
        if event not in (IDLE, CHARGE, DISCHARGE, FORCE_CHARGE, FORCE_DISCHARGE):
            raise ValueError(f"Invalid event code: {event}. Use module constants.")

        schedule = {"event": event, "setpoint": setpoint_watts}
        self._write_schedule_file(schedule)

    # Convenience wrappers

    def idle(self) -> None:
        """Set battery to idle (no forced charge or discharge)."""
        self.set_mode(IDLE)

    def charge(self, watts: int = 0) -> None:
        """Schedule a charge cycle (normal / tariff-driven charging)."""
        self.set_mode(CHARGE, setpoint_watts=watts)

    def discharge(self, watts: int = 0) -> None:
        """Schedule a discharge cycle."""
        self.set_mode(DISCHARGE, setpoint_watts=watts)

    def force_charge(self, watts: int = 0) -> None:
        """Force charge from grid immediately (e.g. during cheap-rate tariff)."""
        self.set_mode(FORCE_CHARGE, setpoint_watts=watts)

    def force_discharge(self, watts: int = 0) -> None:
        """Force discharge to load/grid immediately."""
        self.set_mode(FORCE_DISCHARGE, setpoint_watts=watts)
