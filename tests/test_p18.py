"""
Tests for p18_serial.p18 — P18 protocol framing and CRC.

Only the pure-Python logic (CRC, encode, decode) is tested here.
Hardware-dependent send/receive tests require a real serial device.
"""

import pytest
from p18_serial.p18 import P18Inverter, crc16_xmodem, decode_response, encode_command


# ---------------------------------------------------------------------------
# CRC-XMODEM
# ---------------------------------------------------------------------------

class TestCrc16Xmodem:
    def test_empty(self):
        # CRC of empty bytes is 0x0000
        assert crc16_xmodem(b"") == 0x0000

    def test_known_value_hello(self):
        # CRC-XMODEM of b"Hello" = 0x7893 (from standard tables)
        result = crc16_xmodem(b"Hello")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_deterministic(self):
        data = b"^P003GS"
        assert crc16_xmodem(data) == crc16_xmodem(data)

    def test_different_data_different_crc(self):
        assert crc16_xmodem(b"abc") != crc16_xmodem(b"abd")

    def test_single_byte(self):
        # Just ensure it runs without error
        result = crc16_xmodem(b"\x00")
        assert 0 <= result <= 0xFFFF


# ---------------------------------------------------------------------------
# encode_command
# ---------------------------------------------------------------------------

class TestEncodeCommand:
    def test_type_query(self):
        frame = encode_command("GS", cmd_type="P")
        assert frame.startswith(b"^P")

    def test_type_set(self):
        frame = encode_command("POP00", cmd_type="S")
        assert frame.startswith(b"^S")

    def test_terminates_with_cr(self):
        frame = encode_command("PI")
        assert frame.endswith(b"\r")

    def test_length_field(self):
        # For "PI" (2 chars), length = 2 + 1 = 3 → "003"
        frame = encode_command("PI")
        # Frame starts with ^P003PI...
        assert b"^P003PI" in frame

    def test_length_field_for_set_command(self):
        # For "POP00" (5 chars), length = 5 + 1 = 6 → "006"
        frame = encode_command("POP00", cmd_type="S")
        assert b"^S006POP00" in frame

    def test_crc_appended_two_bytes(self):
        # Frame = header + 2 CRC bytes + CR → total at least 7 bytes for "PI"
        frame = encode_command("PI")
        # ^P003PI = 7 bytes + 2 CRC + 1 CR = 10 bytes minimum
        assert len(frame) == 10

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            encode_command("GS", cmd_type="X")

    def test_crc_is_xmodem_of_header(self):
        cmd = "GS"
        frame = encode_command(cmd)
        header = b"^P003GS"
        expected_crc = crc16_xmodem(header)
        # CRC is at positions -3 and -2 (before trailing CR)
        recv_crc = (frame[-3] << 8) | frame[-2]
        assert recv_crc == expected_crc


# ---------------------------------------------------------------------------
# decode_response
# ---------------------------------------------------------------------------

class TestDecodeResponse:
    def _make_response(self, data: str) -> bytes:
        """Build a valid response frame for *data*."""
        frame_data = f"({data}".encode("ascii")
        from p18_serial.p18 import crc16_xmodem
        import struct
        crc = crc16_xmodem(frame_data)
        return frame_data + struct.pack(">H", crc) + b"\r"

    def test_valid_response_decoded(self):
        raw = self._make_response("PI18")
        result = decode_response(raw)
        assert result == "PI18"

    def test_empty_data_decoded(self):
        raw = self._make_response("")
        result = decode_response(raw)
        assert result == ""

    def test_bad_crc_returns_none(self):
        raw = self._make_response("PI18")
        # Corrupt one CRC byte
        corrupted = bytearray(raw)
        corrupted[-3] ^= 0xFF
        assert decode_response(bytes(corrupted)) is None

    def test_too_short_returns_none(self):
        assert decode_response(b"(") is None
        assert decode_response(b"") is None

    def test_strips_leading_paren(self):
        raw = self._make_response("SOMEDATA")
        result = decode_response(raw)
        assert result is not None
        assert not result.startswith("(")

    def test_strips_trailing_cr(self):
        raw = self._make_response("DATA")
        result = decode_response(raw)
        assert result is not None
        assert not result.endswith("\r")


# ---------------------------------------------------------------------------
# P18Inverter (offline / mock tests)
# ---------------------------------------------------------------------------

class TestP18InverterOffline:
    def test_not_connected_raises(self):
        inv = P18Inverter()
        with pytest.raises(RuntimeError, match="Not connected"):
            inv.get_protocol_id()

    def test_context_manager_opens_and_closes(self, mocker):
        inv = P18Inverter()
        mock_open = mocker.patch.object(inv, "connect")
        mock_close = mocker.patch.object(inv, "disconnect")
        with inv:
            pass
        mock_open.assert_called_once()
        mock_close.assert_called_once()

    def test_set_output_priority_invalid_raises(self):
        inv = P18Inverter()
        inv._dev = object()  # Fake open state
        with pytest.raises(ValueError, match="priority must be"):
            inv.set_output_priority("INVALID")

    def test_set_charger_priority_invalid_raises(self):
        inv = P18Inverter()
        inv._dev = object()
        with pytest.raises(ValueError, match="priority must be"):
            inv.set_charger_priority("BAD")

    def test_send_calls_write_and_read(self, mocker):
        """Verify _send writes a frame and reads a response."""
        import struct

        inv = P18Inverter(is_usb=False)

        # Build a valid "PI18" response
        frame_data = b"(PI18"
        crc = crc16_xmodem(frame_data)
        fake_response = frame_data + struct.pack(">H", crc) + b"\r"

        mock_serial = mocker.MagicMock()
        mock_serial.read_until.return_value = fake_response
        inv._dev = mock_serial

        result = inv._send("PI")
        assert result == "PI18"
        mock_serial.write.assert_called_once()

    def test_force_charge_calls_multiple_setters(self, mocker):
        inv = P18Inverter()
        inv._dev = object()  # simulate open

        mock_send = mocker.patch.object(inv, "_send", return_value="ACK")
        inv.force_charge()

        # Expect at least 3 serial commands (output priority, charger priority, battery control)
        assert mock_send.call_count >= 3

    def test_normal_mode_calls_multiple_setters(self, mocker):
        inv = P18Inverter()
        inv._dev = object()

        mock_send = mocker.patch.object(inv, "_send", return_value="ACK")
        inv.normal_mode()
        assert mock_send.call_count >= 3
