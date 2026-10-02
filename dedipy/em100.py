#!/usr/bin/env python3
"""
EM100Pro — Pure-Python PyUSB driver and CLI for the Dediprog EM100Pro SPI flash emulator.

Usage as library:
    from dedipy import EM100
    with EM100() as dev:
        dev.set_chip("M25P80")
        dev.sdram_write(0, firmware_bytes)
        dev.start()

Usage as CLI:
    em100 --stop --set M25P80 --download fw.bin --verify --start
"""

from __future__ import annotations

import argparse
import json
import pathlib
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import fcntl
import usb.core
import usb.util

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VID = 0x04B4
_PID = 0x1235
_INTERFACE = 0
_EP_OUT = 0x01
_EP_IN = 0x82
_TIMEOUT = 5000
_CMD_LEN = 16
_CHUNK = 0x200000  # 2 MB SDRAM transfer chunk
_BULK_PKT = 512    # SDRAM transfer lengths must be a multiple of this
_SDRAM_ALIGN = 16  # SDRAM transfer addresses must be a multiple of this
_PAGE = 256
_REPORT_BUF_LEN = 8192
_REPORT_BUF_COUNT = 8

# Known internal SPI flash chips: ID -> size in bytes
_SPI_FLASH_IDS = {
    0x202015: 2 * 1024 * 1024,    # Micron M25P16 (original / G2)
    0xC27518: 16 * 1024 * 1024,   # Macronix MX77L12850F (G3 / newer)
}

# Hardware version byte (from serial page offset 1)
HWVERSION_EM100PRO_EARLY = 0xFF
HWVERSION_EM100PRO = 0x04
HWVERSION_EM100PRO_G2 = 0x06

# Flash layout
_FPGA_END = 0x100000
_BOOT_MAGIC_ADDR = 0x100000
_MCU_START = 0x100100
_MCU_END = 0x1F0000
_KEY_ADDR = 0x1F0000
_SERIAL_ADDR = 0x1FFF00
_BOOT_MAGIC = b"\xaa\x55\x42\x4f\x4f\x54\x55\xaa"

# Voltage codes for FPGA
_V18 = 18
_V33 = 33

# LED states
LED_OFF = 0
LED_GREEN = 1
LED_RED = 2
LED_BOTH = 3

# Hold pin states
HOLD_LOW = 0x00
HOLD_FLOAT = 0x02
HOLD_INPUT = 0x03

_HOLD_NAMES = {"low": HOLD_LOW, "float": HOLD_FLOAT, "input": HOLD_INPUT}

# Set voltage channels
SET_TRIG_VCC = 0
SET_RST_VCC = 1
SET_REF_PLUS = 2
SET_REF_MINUS = 3
SET_BUF_VCC = 4

# Measure voltage channels
GET_V1_2 = 0
GET_E_VCC = 1
GET_REF_PLUS = 2
GET_REF_MINUS = 3
GET_BUF_VCC = 4
GET_TRIG_VCC = 5
GET_RST_VCC = 6
GET_V3_3 = 7
GET_BUF_V3_3 = 8
GET_V5 = 9

# HyperTerminal message signature
_HT_SIGNATURE = struct.pack("<I", 0x47364440)  # b'\x40\x44\x36\x47'

# SPI command list for trace decoding: cmd -> (name, uses_address, pad_bytes)
# Merged from em100-yadro, rem100, and Dediprog EM100ProCommandTable.ini.
_SPI_CMDS = {
    0x01: ("write status register", False, 0),
    0x02: ("page program", True, 0),
    0x03: ("read", True, 0),
    0x04: ("write disable", False, 0),
    0x05: ("read status register", False, 0),
    0x06: ("write enable", False, 0),
    0x0B: ("fast read", True, 1),
    0x0C: ("fast read 4B", True, 2),
    0x11: ("EM100 specific", False, 0),
    0x12: ("page program 4B", True, 1),
    0x13: ("read 4B", True, 1),
    0x20: ("sector erase 4KB", True, 0),
    0x21: ("sector erase 4KB 4B", True, 1),
    0x35: ("enter quad I/O mode", False, 0),
    0x38: ("quad page program", True, 0),
    0x3B: ("fast dual read", True, 2),
    0x3C: ("dual out read 4B", True, 2),
    0x3E: ("quad page program 4B", True, 1),
    0x52: ("block erase 32KB", True, 0),
    0x5A: ("read SFDP", True, 0),
    0x5C: ("block erase 32KB 4B", True, 1),
    0x60: ("chip erase", False, 0),
    0x6B: ("quad read", True, 0),
    0x6C: ("quad out read 4B", True, 1),
    0x70: ("read flag status register", False, 0),
    0x9F: ("read JEDEC ID", False, 0),
    0xAB: ("release deep power-down", False, 0),
    0xB7: ("enter 4-byte address mode", False, 0),
    0xBB: ("dual I/O read", True, 2),
    0xBC: ("dual I/O read 4B", True, 2),
    0xC7: ("chip erase", False, 0),
    0xD8: ("sector erase 64KB", True, 0),
    0xDC: ("block erase 64KB 4B", True, 1),
    0xE9: ("exit 4-byte address mode", False, 0),
    0xEB: ("quad I/O read", True, 0),
    0xEC: ("quad I/O read 4B", True, 1),
    0xED: ("quad I/O DT read", True, 0),
    0xEE: ("quad I/O DT read 4B", True, 1),
    0xF5: ("exit quad I/O mode", False, 0),
}


# ---------------------------------------------------------------------------
# SPI transaction
# ---------------------------------------------------------------------------

@dataclass
class SpiTransaction:
    """One decoded SPI bus transaction (command + optional address + data)."""
    timestamp: int          # hardware ticks from trace start (÷ 100_000_000 = seconds)
    command: int            # SPI command byte (e.g. 0x03)
    name: str               # decoded name ("read", "fast read", ...)
    address: int | None     # starting flash address, or None
    data: bytes             # accumulated data bytes


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class EM100Error(Exception):
    """Any non-USB error from the EM100 driver."""


# ---------------------------------------------------------------------------
# Chip database
# ---------------------------------------------------------------------------

def _load_chips() -> dict:
    p = pathlib.Path(__file__).with_name("em100pro_chips.json")
    if not p.exists():
        db = {"version": "unknown", "chips": []}
    else:
        with p.open() as f:
            db = json.load(f)

    # Merge custom chips (override built-in entries with same name).
    # Search next to this file AND in the current working directory.
    custom_paths = [
        pathlib.Path(__file__).with_name("custom_chips.json"),
        pathlib.Path.cwd() / "custom_chips.json",
    ]
    for custom_path in custom_paths:
        if custom_path.exists():
            with custom_path.open() as f:
                custom = json.load(f)
            custom_list = custom if isinstance(custom, list) else custom.get("chips", [])
            # Index built-in chips by lowercase name
            by_name = {c["name"].lower(): i for i, c in enumerate(db["chips"])}
            for cc in custom_list:
                key = cc["name"].lower()
                if key in by_name:
                    db["chips"][by_name[key]] = cc  # override
                else:
                    db["chips"].append(cc)
    return db


# ---------------------------------------------------------------------------
# Trace parser
# ---------------------------------------------------------------------------

@dataclass
class TraceParser:
    """Stateful decoder for SPI trace report buffers."""
    addr_offset: int = 0
    counter: int = 0
    curpos: int = 0
    cmdid: int = 0xFF
    timestamp: int = 0
    start_timestamp: int = 0
    address: int = 0
    outbytes: int = 0
    additional_pad: int = 0
    spi_cmd_name: str = ""
    uses_address: bool = False
    pad_bytes: int = 0
    lines: list = field(default_factory=list)
    all_lines: list = field(default_factory=list)
    transactions: list = field(default_factory=list)
    _pending_cmd: int | None = field(default=None, repr=False)
    _pending_name: str = field(default="", repr=False)
    _pending_addr: int | None = field(default=None, repr=False)
    _pending_ts: int = field(default=0, repr=False)
    _pending_data: bytearray = field(default_factory=bytearray, repr=False)

    def _get_spi_cmd(self, cmd_byte: int):
        name, uses_addr, pad = _SPI_CMDS.get(cmd_byte, ("unknown command", False, 0))
        self.spi_cmd_name = name
        self.uses_address = uses_addr
        self.pad_bytes = pad

    def _finalize_pending(self):
        """Flush in-progress transaction to the transactions list."""
        if self._pending_cmd is not None:
            self.transactions.append(SpiTransaction(
                timestamp=self._pending_ts,
                command=self._pending_cmd,
                name=self._pending_name,
                address=self._pending_addr,
                data=bytes(self._pending_data),
            ))
            self._pending_cmd = None
            self._pending_name = ""
            self._pending_addr = None
            self._pending_ts = 0
            self._pending_data = bytearray()

    def finalize(self):
        """Call once after all data is parsed to flush any remaining transaction."""
        self._finalize_pending()

    def parse_reports(self, report_data: list[bytes]) -> str:
        """Parse a list of 8192-byte report buffers, return formatted text."""
        self.lines = []
        for buf in report_data:
            count = (buf[0] << 8) | buf[1]
            if count > 1022:
                count = 1022
            for i in range(count):
                off = 2 + i * 8
                rec = buf[off:off + 8]
                self._parse_record(rec)
        text = "".join(self.lines)
        self.all_lines.append(text)
        return text

    def _parse_record(self, rec: bytes):
        cmd = rec[0]
        if cmd == 0xFF:
            # timestamp record
            self.timestamp = int.from_bytes(rec[2:8], "big")
            return

        j = self.additional_pad
        self.additional_pad = 0

        if cmd != self.cmdid:
            self._finalize_pending()
            spi_command = rec[2]
            self._get_spi_cmd(spi_command)
            self.cmdid = cmd
            if self.counter == 0:
                self.start_timestamp = self.timestamp
            if not self.uses_address:
                j = 1
                self._pending_addr = None
            else:
                self.address = (rec[3] << 16) | (rec[4] << 8) | rec[5]
                j = 4 + self.pad_bytes
                if j > 6:
                    self.additional_pad = j - 6
                    j = 6
                self._pending_addr = self.address
            dt = self.timestamp - self.start_timestamp
            self.counter += 1
            self.lines.append(
                f"\nTime: {dt // 100000000:06d}.{dt % 100000000:08d}"
                f" command # {self.counter:<6d} : 0x{spi_command:02x} - {self.spi_cmd_name}"
            )
            self.curpos = 0
            self.outbytes = 0
            self._pending_cmd = spi_command
            self._pending_name = self.spi_cmd_name
            self._pending_ts = dt

        blocklen = ((rec[1] - self.curpos) & 0xFF) // 8
        if blocklen > 6:
            blocklen = 6  # max 6 data bytes per 8-byte record

        for idx in range(j, blocklen):
            if self.outbytes == 0:
                if self.uses_address:
                    self.lines.append(f"\n{self.addr_offset + self.address:08x} : ")
                else:
                    self.lines.append("\n         : ")
            data_byte = rec[2 + idx]
            self.lines.append(f"{data_byte:02x} ")
            self._pending_data.append(data_byte)
            self.outbytes += 1
            if self.outbytes == 16:
                self.outbytes = 0
                if self.uses_address:
                    self.address += 16

        self.curpos = (rec[1] + 0x10) & 0xFF


# ---------------------------------------------------------------------------
# Trace capture context manager
# ---------------------------------------------------------------------------

class TraceCapture:
    """Context manager that polls SPI trace data in a background thread.

    Usage::

        with TraceCapture(dev) as cap:
            # emulation is running, trace polling is active
            input("Power on DuT, press Enter when done...")

        for txn in cap.transactions:
            print(txn)
    """

    def __init__(self, dev: "EM100"):
        self._dev = dev
        self._parser = TraceParser()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def parser(self) -> TraceParser:
        """The underlying TraceParser (available after exiting the context)."""
        return self._parser

    @property
    def transactions(self) -> list[SpiTransaction]:
        """Structured SPI transactions captured during the session."""
        return self._parser.transactions

    @property
    def text(self) -> str:
        """All trace text accumulated across polling rounds."""
        return "".join(self._parser.all_lines)

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "TraceCapture":
        self._dev.trace_reset()
        self._dev.start()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        self._thread.join()
        self._thread = None
        self._dev.stop()
        # Final drain — pick up anything buffered after the stop-event
        try:
            bufs = self._dev.trace_read_raw()
            self._parser.parse_reports(bufs)
        except usb.core.USBError:
            pass
        self._parser.finalize()
        return False

    # -- background thread ---------------------------------------------------

    def _poll(self):
        while not self._stop_event.is_set():
            try:
                bufs = self._dev.trace_read_raw()
                self._parser.parse_reports(bufs)
            except usb.core.USBError:
                # Back off briefly to avoid spinning on persistent errors
                self._stop_event.wait(0.05)


# ---------------------------------------------------------------------------
# HyperTerminal message parser
# ---------------------------------------------------------------------------

def _parse_ht_messages(data: bytes) -> list[str]:
    """Parse HyperTerminal messages from uFIFO data. Returns list of formatted strings."""
    if len(data) < 2:
        return []
    valid_len = (data[0] << 8) | data[1]
    if valid_len == 0:
        return []

    payload = data[2:2 + valid_len]
    results = []
    j = 0
    while j < len(payload) - 5:  # need at least header (6 bytes)
        if payload[j:j + 4] != _HT_SIGNATURE:
            j += 1
            continue
        data_type = payload[j + 4]
        data_length = payload[j + 5]
        msg_data = payload[j + 6:j + 6 + data_length]
        if data_type == 0x05:  # ASCII
            results.append(msg_data.decode("ascii", errors="replace"))
        elif data_type == 0x07:  # lookup table
            parts = []
            for k in range(0, len(msg_data) - 1, 2):
                parts.append(f"{msg_data[k]:02x}{msg_data[k + 1]:02x}")
            results.append("Lookup: " + " ".join(parts))
        else:  # hex types (checkpoint 1/2/4, hex data, timestamp)
            results.append(" ".join(f"{b:02x}" for b in msg_data))
        j += 6 + data_length
    return results


# ---------------------------------------------------------------------------
# EM100 driver class
# ---------------------------------------------------------------------------

class EM100:
    """Driver for the Dediprog EM100Pro SPI flash emulator."""

    def __init__(self, serial: Optional[int] = None, bus_device: Optional[tuple[int, int]] = None):
        """Open connection to an EM100Pro device.

        Args:
            serial: Device serial number (integer, e.g. 123456 for DP123456).
            bus_device: Tuple of (bus, device) to select a specific USB device.
        """
        self._dev: usb.core.Device = None
        self._mcu: int = 0
        self._fpga: int = 0
        self._serial: int = 0
        self._hwversion: int = 0x00
        self._spi_flash_id: int = 0
        self._spi_flash_size: int = 0
        self._chips_db = _load_chips()

        if bus_device:
            bus, addr = bus_device
            dev = usb.core.find(idVendor=_VID, idProduct=_PID,
                                bus=bus, address=addr)
            if dev is None:
                raise EM100Error(f"No EM100Pro at bus {bus:03d}:{addr:03d}")
        elif serial:
            # Must enumerate all and check serial numbers
            dev = self._find_by_serial(serial)
        else:
            dev = usb.core.find(idVendor=_VID, idProduct=_PID)

        if dev is None:
            raise EM100Error("No EM100Pro device found")

        self._dev = dev
        try:
            if dev.is_kernel_driver_active(_INTERFACE):
                dev.detach_kernel_driver(_INTERFACE)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError:
            # On Linux the kernel often already set the (sole) configuration;
            # re-setting an active config can return NOT_FOUND/BUSY. Tolerate it
            # as long as a configuration is actually active.
            dev.get_active_configuration()
        usb.util.claim_interface(dev, _INTERFACE)
        # Clear any stale endpoint halt from a previously aborted transfer;
        # otherwise the first bulk transfer can fail with EIO.
        for _ep in (_EP_OUT, _EP_IN):
            try:
                dev.clear_halt(_ep)
            except usb.core.USBError:
                pass

        # Verify device and determine internal SPI flash size.
        self._spi_flash_id = self.spi_id()
        if self._spi_flash_id in (0x000000, 0xFFFFFF):
            raise EM100Error(f"No SPI flash detected (ID: 0x{self._spi_flash_id:06x})")
        if self._spi_flash_id not in _SPI_FLASH_IDS:
            raise EM100Error(
                f"Unknown SPI flash ID: 0x{self._spi_flash_id:06x}. Please report."
            )
        self._spi_flash_size = _SPI_FLASH_IDS[self._spi_flash_id]

        self._read_version()
        self._read_device_info()

    def _find_by_serial(self, target_serial: int) -> usb.core.Device:
        devices = list(usb.core.find(idVendor=_VID, idProduct=_PID, find_all=True))
        for dev in devices:
            try:
                if dev.is_kernel_driver_active(_INTERFACE):
                    dev.detach_kernel_driver(_INTERFACE)
            except (usb.core.USBError, NotImplementedError):
                pass
            try:
                dev.set_configuration()
                usb.util.claim_interface(dev, _INTERFACE)
                # Read serial from SPI flash
                page = self._spi_read_page_raw(dev, _SERIAL_ADDR)
                serno = struct.unpack_from("<I", page, 2)[0]
                if serno == target_serial:
                    return dev
                usb.util.release_interface(dev, _INTERFACE)
            except usb.core.USBError:
                continue
        raise EM100Error(f"No EM100Pro with serial {target_serial:06d}")

    @staticmethod
    def _spi_read_page_raw(dev: usb.core.Device, addr: int) -> bytes:
        """Read a SPI flash page using raw USB ops (for init before self._dev is set)."""
        cmd = bytearray(_CMD_LEN)
        cmd[0] = 0x33
        cmd[1] = (addr >> 16) & 0xFF
        cmd[2] = (addr >> 8) & 0xFF
        cmd[3] = addr & 0xFF
        dev.write(_EP_OUT, cmd, timeout=_TIMEOUT)
        return dev.read(_EP_IN, _PAGE, timeout=_TIMEOUT).tobytes()

    def usb_reset(self):
        """Force USB re-enumeration via USBDEVFS_RESET ioctl.

        After calling this the EM100 object is invalid — create a new one.
        """
        if self._dev is None:
            return
        bus = self._dev.bus
        addr = self._dev.address
        self.close()
        dev_path = f"/dev/bus/usb/{bus:03d}/{addr:03d}"
        USBDEVFS_RESET = 0x5514
        with open(dev_path, 'wb') as fd:
            fcntl.ioctl(fd.fileno(), USBDEVFS_RESET, 0)
        time.sleep(2)  # wait for re-enumeration

    def close(self):
        """Release the USB interface and close the device."""
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, _INTERFACE)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(self._dev)
            self._dev = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- Properties --

    @property
    def serial(self) -> int:
        return self._serial

    @property
    def serial_string(self) -> str:
        """Serial number as display string (e.g. 'DP000123' or 'EM000456')."""
        if self._serial == 0xFFFFFFFF:
            return "N.A."
        prefix = "DP" if self._hwversion == HWVERSION_EM100PRO_EARLY else "EM"
        return f"{prefix}{self._serial:06d}"

    @property
    def hwversion(self) -> int:
        """Hardware version byte (0xFF=early, 0x04=original, 0x06=G2)."""
        return self._hwversion

    @property
    def spi_flash_size(self) -> int:
        """Internal SPI flash size in bytes."""
        return self._spi_flash_size

    @property
    def mcu_version(self) -> str:
        return f"{self._mcu >> 8}.{self._mcu & 0xFF:02d}"

    @property
    def fpga_version(self) -> str:
        major = (self._fpga >> 8) & 0x7F
        minor = self._fpga & 0xFF
        if self._hwversion in (HWVERSION_EM100PRO_EARLY, HWVERSION_EM100PRO):
            return f"{major}.{minor:02d}"
        if self._hwversion == HWVERSION_EM100PRO_G2:
            return f"{major}.{minor:03d}"
        # Unknown hardware — no padding, matching rem100 behavior
        return f"{major}.{minor}"

    @property
    def fpga_voltage(self) -> float:
        return 1.8 if (self._fpga & 0x8000) else 3.3

    # -----------------------------------------------------------------------
    # Transport — the entire USB layer (4 methods)
    # -----------------------------------------------------------------------

    def _cmd(self, *args: int):
        """Send a 16-byte zero-padded command, no response expected."""
        buf = bytearray(_CMD_LEN)
        for i, b in enumerate(args):
            buf[i] = b & 0xFF
        self._dev.write(_EP_OUT, buf, timeout=_TIMEOUT)

    def _cmd_resp(self, *args: int, rlen: int = 512) -> bytes:
        """Send command and read response."""
        self._cmd(*args)
        return self._dev.read(_EP_IN, rlen, timeout=_TIMEOUT).tobytes()

    def _bulk_write(self, data: bytes | bytearray):
        """Raw bulk write on EP1 OUT."""
        self._dev.write(_EP_OUT, data, timeout=_TIMEOUT)

    def _bulk_read(self, length: int) -> bytes:
        """Raw bulk read on EP2 IN."""
        return self._dev.read(_EP_IN, length, timeout=_TIMEOUT).tobytes()

    # -----------------------------------------------------------------------
    # Lifecycle helpers
    # -----------------------------------------------------------------------

    def _read_version(self):
        resp = self._cmd_resp(0x10, rlen=5)
        if len(resp) == 5 and resp[0] == 4:
            self._fpga = (resp[1] << 8) | resp[2]
            self._mcu = (resp[3] << 8) | resp[4]
        else:
            raise EM100Error("Failed to read version")

    def _read_device_info(self):
        """Read serial number and hardware version from SPI flash."""
        page = self.spi_read_page(_SERIAL_ADDR)
        self._hwversion = page[1]
        self._serial = struct.unpack_from("<I", page, 2)[0]

    # -----------------------------------------------------------------------
    # System commands (0x10-0x13)
    # -----------------------------------------------------------------------

    def get_version(self) -> dict:
        """Return MCU and FPGA version info."""
        self._read_version()
        return {
            "mcu": self.mcu_version,
            "fpga": self.fpga_version,
            "fpga_voltage": self.fpga_voltage,
        }

    def set_voltage(self, channel: int, mV: int):
        """Set output voltage on a channel.

        For channels 0-3: mV is actual millivolts.
        For channel 4 (Buffer VCC): use codes 18, 25, or 33.
        """
        if channel == SET_BUF_VCC and mV not in (18, 25, 33):
            raise EM100Error("Buffer VCC must be 18 (1.8V), 25 (2.5V), or 33 (3.3V)")
        self._cmd(0x11, channel, (mV >> 8) & 0xFF, mV & 0xFF)

    def get_voltage(self, channel: int) -> int:
        """Measure voltage on a channel. Returns millivolts."""
        resp = self._cmd_resp(0x12, channel, rlen=3)
        if len(resp) == 3 and resp[0] == 2:
            raw = (resp[1] << 8) | resp[2]
            if channel <= 3:
                return raw * 12207 // 10000
            return raw * 48828 // 10000
        raise EM100Error(f"Failed to read voltage on channel {channel}")

    def set_led(self, state: int):
        """Set LED state: 0=off, 1=green, 2=red, 3=both."""
        self._cmd(0x13, state)

    # -----------------------------------------------------------------------
    # FPGA commands (0x20-0x24)
    # -----------------------------------------------------------------------

    def fpga_reconfig(self):
        """Reconfigure FPGA. Waits 2 seconds after."""
        self._cmd(0x20)
        time.sleep(2)

    def fpga_status(self) -> bool:
        """Check FPGA configuration status. Returns True if passed."""
        resp = self._cmd_resp(0x21, rlen=1)
        return len(resp) == 1 and resp[0] == 1

    def fpga_read(self, reg: int) -> int:
        """Read a 16-bit FPGA register."""
        resp = self._cmd_resp(0x22, reg, rlen=3)
        if len(resp) == 3 and resp[0] == 2:
            return (resp[1] << 8) | resp[2]
        raise EM100Error(f"Failed to read FPGA register 0x{reg:02x}")

    def fpga_write(self, reg: int, val: int):
        """Write a 16-bit FPGA register."""
        self._cmd(0x23, reg, (val >> 8) & 0xFF, val & 0xFF)

    def fpga_set_voltage(self, voltage_code: int):
        """Switch FPGA voltage (G2 only). voltage_code: 18 for 1.8V, 33 for 3.3V.

        Full ceremony: reconfig → set → sleep(2) → verify.
        """
        if voltage_code not in (_V18, _V33):
            raise EM100Error("Voltage code must be 18 or 33")

        # Step 1: reconfig (no sleep)
        self._cmd(0x20)

        # Step 2: set voltage
        if voltage_code == _V18:
            self._cmd(0x24, 0x00, 0x07, 0x80)
        else:
            self._cmd(0x24)

        # Step 3: mandatory 2s delay
        time.sleep(2)

        # Step 4-5: verify via get_version
        self._read_version()
        actual = _V18 if (self._fpga & 0x8000) else _V33
        if actual != voltage_code:
            raise EM100Error(
                f"FPGA voltage switch failed: got {actual}, expected {voltage_code}"
            )

    # -----------------------------------------------------------------------
    # Emulation control
    # -----------------------------------------------------------------------

    def start(self):
        """Start SPI emulation."""
        self.fpga_write(0x28, 1)

    def stop(self):
        """Stop SPI emulation."""
        self.fpga_write(0x28, 0)

    def is_running(self) -> bool:
        """Check if emulation is running."""
        return self.fpga_read(0x28) != 0

    def set_address_mode(self, mode: int):
        """Set SPI address mode (3 or 4 byte). FPGA register 0x4F."""
        if mode not in (3, 4):
            raise EM100Error(f"Invalid address mode: {mode}")
        self.fpga_write(0x4F, 1 if mode == 4 else 0)

    def set_hold_pin(self, state: int):
        """Set hold pin state using read-ack-write-verify ceremony.

        state: HOLD_LOW (0), HOLD_FLOAT (2), or HOLD_INPUT (3).
        """
        # Step 1: read current value
        cur = self.fpga_read(0x2A)
        # Step 2: acknowledge by setting bit 2
        self.fpga_write(0x2A, cur | 0x04)
        # Step 3: re-read after ack
        self.fpga_read(0x2A)
        # Step 4: write desired state
        self.fpga_write(0x2A, state)
        # Step 5: verify
        actual = self.fpga_read(0x2A)
        if actual != state:
            raise EM100Error(
                f"Hold pin verify failed: got 0x{actual:04x}, expected 0x{state:04x}"
            )

    # -----------------------------------------------------------------------
    # SPI flash — device internal (0x30-0x37)
    # -----------------------------------------------------------------------

    def spi_id(self) -> int:
        """Get internal SPI flash ID (3 bytes as integer)."""
        resp = self._cmd_resp(0x30, rlen=3)
        if len(resp) == 3:
            return (resp[0] << 16) | (resp[1] << 8) | resp[2]
        raise EM100Error("Failed to read SPI flash ID")

    def spi_erase(self):
        """Erase entire internal SPI flash. Waits 5 seconds."""
        self._cmd(0x31)
        time.sleep(5)

    def spi_poll(self) -> bool:
        """Poll SPI flash status. Returns True if ready."""
        resp = self._cmd_resp(0x32, rlen=1)
        return len(resp) == 1 and resp[0] == 1

    def spi_read_page(self, addr: int) -> bytes:
        """Read 256-byte page from internal SPI flash."""
        resp = self._cmd_resp(
            0x33, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
            rlen=_PAGE,
        )
        if len(resp) == _PAGE:
            return bytes(resp)
        raise EM100Error(f"SPI read page failed at 0x{addr:06x}: got {len(resp)} bytes")

    def spi_write_page(self, addr: int, data: bytes):
        """Write 256-byte page to internal SPI flash."""
        if len(data) != _PAGE:
            raise EM100Error(f"Page must be {_PAGE} bytes, got {len(data)}")
        self._cmd(0x34, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF)
        self._bulk_write(data)

    def spi_unlock(self):
        """Unlock internal SPI flash for writing."""
        self._cmd(0x36)

    def spi_erase_sector(self, sector: int):
        """Erase a 64KB sector of internal SPI flash."""
        max_sector = self._spi_flash_size // (64 * 1024) - 1
        if sector < 0 or sector > max_sector:
            raise EM100Error(f"Sector must be 0-{max_sector}, got {sector}")
        self._cmd(0x37, sector)

    # -----------------------------------------------------------------------
    # SDRAM — emulation memory (0x40-0x41)
    # -----------------------------------------------------------------------

    # Hardware constraints (measured on EM100Pro G3, MCU 3.10 / FPGA 3.18):
    #  - Transfer lengths must be a multiple of the 512-byte bulk packet size.
    #    A write ending in a short packet hangs the firmware (it stops answering
    #    even USB enumeration until power-cycled); a read always returns whole
    #    packets, overflowing the host buffer and leaving stale data in EP2 IN.
    #  - Addresses must be 16-byte aligned (one SDRAM burst of 8 x 16-bit words).
    #    Writes silently land at address & ~0xF; reads with address bits [3:1]
    #    set return each 16-byte block with its words permuted.
    # The public methods below accept any address/length and map them onto
    # aligned transfers, preserving the surrounding SDRAM contents.

    def _sdram_xfer_cmd(self, op: int, address: int, length: int):
        if address % _SDRAM_ALIGN or length % _BULK_PKT:
            raise EM100Error(
                f"Unaligned SDRAM transfer: address 0x{address:x}, length {length}"
            )
        self._cmd(
            op,
            (address >> 24) & 0xFF, (address >> 16) & 0xFF,
            (address >> 8) & 0xFF, address & 0xFF,
            (length >> 24) & 0xFF, (length >> 16) & 0xFF,
            (length >> 8) & 0xFF, length & 0xFF,
        )

    def _sdram_write_aligned(self, address: int, data: bytes):
        length = len(data)
        self._sdram_xfer_cmd(0x40, address, length)
        sent = 0
        while sent < length:
            chunk = min(_CHUNK, length - sent)
            self._bulk_write(data[sent:sent + chunk])
            sent += chunk

    def _sdram_read_aligned(self, address: int, length: int) -> bytes:
        self._sdram_xfer_cmd(0x41, address, length)
        parts = []
        remaining = length
        while remaining > 0:
            chunk = min(_CHUNK, remaining)
            parts.append(self._bulk_read(chunk))
            remaining -= len(parts[-1])
            if len(parts[-1]) < chunk:
                break
        return b"".join(parts)

    def sdram_write(self, address: int, data: bytes):
        """Write data to SDRAM at any address/length with automatic 2MB chunking.

        Unaligned head/tail bytes are filled with the current SDRAM contents
        (read-modify-write), so nothing outside [address, address+len) changes.
        """
        if not data:
            return
        start = address & ~(_SDRAM_ALIGN - 1)
        end = address + len(data)
        stop = end + (-(end - start) % _BULK_PKT)
        head = self.sdram_read(start, address - start) if address > start else b""
        tail = self.sdram_read(end, stop - end) if stop > end else b""
        self._sdram_write_aligned(start, head + bytes(data) + tail)

    def sdram_read(self, address: int, length: int) -> bytes:
        """Read data from SDRAM at any address/length with automatic 2MB chunking."""
        if length <= 0:
            return b""
        start = address & ~(_SDRAM_ALIGN - 1)
        n = address + length - start
        n += -n % _BULK_PKT
        raw = self._sdram_read_aligned(start, n)
        return raw[address - start:address - start + length]

    # -----------------------------------------------------------------------
    # HyperTerminal (0x50-0x53)
    # -----------------------------------------------------------------------

    def ht_read_reg(self, reg: int) -> int:
        """Read a HyperTerminal register (1 byte)."""
        resp = self._cmd_resp(0x50, reg, rlen=2)
        if len(resp) == 2 and resp[0] == 1:
            return resp[1]
        raise EM100Error(f"Failed to read HT register {reg}")

    def ht_write_reg(self, reg: int, val: int):
        """Write a HyperTerminal register."""
        self._cmd(0x51, reg, val & 0xFF)

    def ht_write_dfifo(self, data: bytes, timeout_ms: int = 0):
        """Write data to downstream FIFO (host → target). Max 512 bytes."""
        length = len(data)
        if length > 512:
            raise EM100Error("dFIFO data cannot exceed 512 bytes")
        self._cmd(
            0x52,
            (length >> 8) & 0xFF, length & 0xFF,
            (timeout_ms >> 8) & 0xFF, timeout_ms & 0xFF,
        )
        # Send caller's data (fixes C bug #1)
        self._bulk_write(data)
        # Read response (byte count)
        try:
            self._bulk_read(512)
        except usb.core.USBError:
            pass

    def ht_read_ufifo(self, length: int = 512, timeout_ms: int = 0) -> bytes:
        """Read data from upstream FIFO (target → host). Max 512 bytes."""
        if length > 512:
            raise EM100Error("uFIFO read length cannot exceed 512 bytes")
        resp = self._cmd_resp(
            0x53,
            (length >> 8) & 0xFF, length & 0xFF,
            (timeout_ms >> 8) & 0xFF, timeout_ms & 0xFF,
            rlen=length,
        )
        # Second read required by protocol (consumed and discarded)
        try:
            self._bulk_read(2)
        except usb.core.USBError:
            pass
        return bytes(resp)

    def ht_init(self):
        """Initialize HyperTerminal / SPI trace terminal mode."""
        self.ht_write_reg(4, 0x00)   # uFIFO data format = raw
        self.ht_write_reg(0, 0x10)   # start SPI emulation
        self.fpga_write(0x82, 0x11)  # set HT SPI command
        self.fpga_read(0x28)         # read emulation state

    # -----------------------------------------------------------------------
    # SPI Trace (0xBC-0xBD)
    # -----------------------------------------------------------------------

    def trace_reset(self):
        """Reset/clear the SPI trace buffer."""
        self._cmd(0xBD)

    def trace_read_raw(self) -> list[bytes]:
        """Read raw trace report buffers (8 x 8192 bytes). Returns list of buffers."""
        self._cmd(
            0xBC,
            0x00, 0x00, 0x00, _REPORT_BUF_COUNT,  # trace_len
            0x00, 0x00, 0x00, 0x00,                # timeout
            0x15,                                   # trace config
        )
        buffers = []
        for _ in range(_REPORT_BUF_COUNT):
            buf = self._bulk_read(_REPORT_BUF_LEN)
            if len(buf) != _REPORT_BUF_LEN:
                raise EM100Error(
                    f"Trace report size {len(buf)} != {_REPORT_BUF_LEN}"
                )
            buffers.append(buf)
        return buffers

    # -----------------------------------------------------------------------
    # Chip configuration
    # -----------------------------------------------------------------------

    def list_chips(self) -> list[dict]:
        """Return list of supported chips."""
        return self._chips_db.get("chips", [])

    def set_chip(self, name: str):
        """Configure emulation for a specific chip by name.

        Performs voltage compatibility check, sends init sequence,
        and writes post-init FPGA registers.
        """
        chip = None
        for c in self._chips_db.get("chips", []):
            if c["name"].lower() == name.lower():
                chip = c
                break
        if chip is None:
            raise EM100Error(f"Unknown chip: {name}")

        # Voltage compatibility check
        fpga_mv = 1800 if (self._fpga & 0x8000) else 3300
        for entry in chip["init"]:
            if entry[0] == 0x11 and entry[1] == 0x04:
                chip_voltage = (entry[2] << 8) | entry[3]
                if chip_voltage in (1601, 1800) and fpga_mv == 3300:
                    raise EM100Error(
                        f"FPGA is 3.3V but {chip['vendor']} {chip['name']} "
                        f"requires {chip_voltage / 1000:.1f}V"
                    )
                if chip_voltage == 3300 and fpga_mv == 1800:
                    raise EM100Error(
                        f"FPGA is 1.8V but {chip['vendor']} {chip['name']} "
                        f"requires 3.3V"
                    )
                break

        # Send init entries as raw 16-byte commands
        for entry in chip["init"]:
            cmd = bytearray(_CMD_LEN)
            for i, b in enumerate(entry):
                cmd[i] = b
            self._dev.write(_EP_OUT, cmd, timeout=_TIMEOUT)

        # Post-init FPGA registers
        self.fpga_write(0xC4, 0x01)
        self.fpga_write(0x10, 0x00)
        self.fpga_write(0x81, 0x00)

    # -----------------------------------------------------------------------
    # Firmware dump / update
    # -----------------------------------------------------------------------

    def firmware_dump(self, dpfw: bool = False) -> bytes:
        """Read entire internal SPI flash.

        If dpfw=True, returns a DPFW-formatted file.
        Otherwise returns the raw image (size depends on hardware).
        """
        rom_size = self._spi_flash_size
        rom = bytearray(rom_size)
        for addr in range(0, rom_size, _PAGE):
            # Triple-retry matching C reference
            for attempt in range(3):
                try:
                    page = self.spi_read_page(addr)
                    break
                except EM100Error:
                    if attempt == 2:
                        page = b"\xff" * _PAGE
            rom[addr:addr + _PAGE] = page

        if not dpfw:
            return bytes(rom)

        # Build DPFW file
        all_ff = b"\xff" * _PAGE
        fpga_size = 0
        for i in range(0, _FPGA_END, _PAGE):
            if rom[i:i + _PAGE] == all_ff:
                break
            fpga_size = i + _PAGE

        mcu_size = 0
        for i in range(0, _MCU_END - _MCU_START, _PAGE):
            if rom[_MCU_START + i:_MCU_START + i + _PAGE] == all_ff:
                break
            mcu_size = i + _PAGE

        # Use %d format (no zero-padding) to match C reference's snprintf "%d.%d"
        mcu_ver = f"{self._mcu >> 8}.{self._mcu & 0xFF}"[:4]
        fpga_ver = f"{(self._fpga >> 8) & 0x7F}.{self._fpga & 0xFF}"[:4]

        header = bytearray(0x100)
        # DPFW magic depends on hardware version
        if self._hwversion == HWVERSION_EM100PRO_G2:
            header[0:11] = b"EM100Pro-G2"
        else:
            header[0:8] = b"em100pro\x00"[:8]
        header[0x14:0x18] = mcu_ver.encode("ascii").ljust(4, b"\x00")[:4]
        header[0x1E:0x22] = fpga_ver.encode("ascii").ljust(4, b"\x00")[:4]
        header[0x28:0x2C] = b"WFPD"
        struct.pack_into("<I", header, 0x38, 0x100)
        struct.pack_into("<I", header, 0x3C, fpga_size)
        struct.pack_into("<I", header, 0x40, 0x100 + fpga_size)
        struct.pack_into("<I", header, 0x44, mcu_size)  # Fixes C bug: writes actual size

        return bytes(header) + bytes(rom[:fpga_size]) + bytes(rom[_MCU_START:_MCU_START + mcu_size])

    def firmware_update(self, fw_data: bytes, verify: bool = False):
        """Update firmware from a DPFW file.

        Args:
            fw_data: Contents of a .dpfw firmware file.
            verify: If True, read back and verify after writing.
        """
        if len(fw_data) < 0x48:
            raise EM100Error("Firmware file too short")
        if fw_data[0x28:0x2C] != b"WFPD":
            raise EM100Error("Not a valid EM100Pro DPFW firmware file")
        if self._hwversion == HWVERSION_EM100PRO_G2:
            if fw_data[:11] != b"EM100Pro-G2":
                raise EM100Error("Not an EM100Pro-G2 firmware file")
        else:
            if fw_data[:8] != b"em100pro":
                raise EM100Error("Not a valid EM100Pro DPFW firmware file")

        fpga_offset = struct.unpack_from("<I", fw_data, 0x38)[0]
        fpga_len = struct.unpack_from("<I", fw_data, 0x3C)[0]
        mcu_offset = struct.unpack_from("<I", fw_data, 0x40)[0]
        mcu_len = struct.unpack_from("<I", fw_data, 0x44)[0]

        if fpga_len < 256 or mcu_len < 256 or fpga_len > 0x100000 or mcu_len > 0xF0000:
            raise EM100Error("Firmware file dimensions invalid")

        # Unlock and erase
        self.spi_unlock()
        self.spi_id()
        for sector in range(0x1F):
            self.spi_erase_sector(sector)

        self.spi_id()

        # Write FPGA firmware
        for i in range(0, fpga_len, _PAGE):
            page = bytearray(b"\xff" * _PAGE)
            end = min(fpga_len - i, _PAGE)
            page[:end] = fw_data[fpga_offset + i:fpga_offset + i + end]
            self.spi_write_page(i, bytes(page))

        # Write MCU firmware
        for i in range(0, mcu_len, _PAGE):
            page = bytearray(b"\xff" * _PAGE)
            end = min(mcu_len - i, _PAGE)
            page[:end] = fw_data[mcu_offset + i:mcu_offset + i + end]
            self.spi_write_page(_MCU_START + i, bytes(page))

        if verify:
            # Verify FPGA
            for i in range(0, fpga_len, _PAGE):
                page = bytearray(b"\xff" * _PAGE)
                end = min(fpga_len - i, _PAGE)
                page[:end] = fw_data[fpga_offset + i:fpga_offset + i + end]
                vpage = self.spi_read_page(i)
                if bytes(page) != vpage:
                    raise EM100Error(f"FPGA verify failed at 0x{i:06x}")

            # Verify MCU
            for i in range(0, mcu_len, _PAGE):
                page = bytearray(b"\xff" * _PAGE)
                end = min(mcu_len - i, _PAGE)
                page[:end] = fw_data[mcu_offset + i:mcu_offset + i + end]
                vpage = self.spi_read_page(_MCU_START + i)
                if bytes(page) != vpage:
                    raise EM100Error(f"MCU verify failed at 0x{i:06x}")

        # Write boot magic page
        boot_page = bytearray(_PAGE)
        boot_page[:8] = _BOOT_MAGIC
        self.spi_write_page(_BOOT_MAGIC_ADDR, bytes(boot_page))

        if verify:
            vpage = self.spi_read_page(_BOOT_MAGIC_ADDR)
            if bytes(boot_page) != vpage:
                raise EM100Error("Boot magic verify failed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _hold_pin_name(val: int) -> str:
    return {HOLD_LOW: "low", HOLD_FLOAT: "float", HOLD_INPUT: "input"}.get(val, "unknown")


def main():
    p = argparse.ArgumentParser(
        prog="em100",
        description="EM100Pro SPI flash emulator command line tool",
        epilog="Example: em100.py --stop --set M25P80 --download fw.bin --verify --start",
    )
    p.add_argument("-c", "--set", metavar="CHIP", help="select chip emulation")
    p.add_argument("-d", "--download", metavar="FILE", help="download FILE into EM100Pro SDRAM")
    p.add_argument("-u", "--upload", metavar="FILE", help="upload EM100Pro SDRAM content to FILE")
    p.add_argument("-r", "--start", action="store_true", help="start emulation")
    p.add_argument("-s", "--stop", action="store_true", help="stop emulation")
    p.add_argument("-v", "--verify", action="store_true", help="verify SDRAM content matches file")
    p.add_argument("-t", "--trace", action="store_true", help="SPI trace mode")
    p.add_argument("-O", "--offset", metavar="HEX", default="0", help="address offset for trace")
    p.add_argument("-T", "--terminal", action="store_true", help="HyperTerminal mode")
    p.add_argument("-F", "--firmware-update", metavar="FILE", help="update firmware (dangerous)")
    p.add_argument("-f", "--firmware-dump", metavar="FILE", help="dump raw firmware to file")
    p.add_argument("-g", "--firmware-write", metavar="FILE", help="dump firmware as DPFW file")
    p.add_argument("-S", "--set-serialno", metavar="NUM", help="set serial number")
    p.add_argument("-V", "--set-voltage", metavar="V", choices=["1.8", "3.3"],
                   help="switch FPGA voltage (1.8 or 3.3)")
    p.add_argument("-p", "--holdpin", metavar="STATE", choices=["LOW", "FLOAT", "INPUT"],
                   help="set hold pin state")
    p.add_argument("-x", "--device", metavar="BUS:DEV|EMxxxxxx",
                   help="select device by bus:dev or serial (DP/EM prefix accepted)")
    p.add_argument("-l", "--list-devices", action="store_true", help="list connected EM100Pro devices")
    p.add_argument("--list-chips", action="store_true", help="list supported chip types")
    p.add_argument("-m", "--address-mode", metavar="MODE", type=int, choices=[3, 4],
                   help="force 3 or 4 byte SPI address mode")
    p.add_argument("-D", "--debug", action="store_true", help="print debug information")

    args = p.parse_args()

    # --list-chips doesn't need hardware
    if args.list_chips:
        db = _load_chips()
        for c in db.get("chips", []):
            print(f"  {c['vendor']:20s} {c['name']:20s} ({c['size'] // 1024:>5d} KB)")
        print(f"\n{len(db.get('chips', []))} chips (database version {db.get('version', '?')})")
        return

    # --list-devices: enumerate all
    if args.list_devices:
        devices = list(usb.core.find(idVendor=_VID, idProduct=_PID, find_all=True))
        if not devices:
            print("No EM100Pro devices found.")
            return
        for dev in devices:
            try:
                em = EM100(bus_device=(dev.bus, dev.address))
                print(f"  Bus {dev.bus:03d} Device {dev.address:03d}: EM100Pro {em.serial_string}")
                em.close()
            except (EM100Error, usb.core.USBError) as e:
                print(f"  Bus {dev.bus:03d} Device {dev.address:03d}: (error: {e})")
        return

    # Parse --device
    serial = None
    bus_device = None
    if args.device:
        d = args.device.strip()
        if d.upper().startswith("DP") or d.upper().startswith("EM"):
            serial = int(d[2:])
        elif ":" in d:
            parts = d.split(":")
            bus_device = (int(parts[0]), int(parts[1]))
        else:
            serial = int(d)

    # Validate chip name before opening device
    if args.set:
        db = _load_chips()
        found = any(c["name"].lower() == args.set.lower() for c in db.get("chips", []))
        if not found:
            chips = db.get("chips", [])
            print("Supported chips:")
            for c in chips:
                print(f"  {c['name']}", end="")
            print(f"\n\nCould not find emulation for '{args.set}'.")
            return

    # Open device
    try:
        em = EM100(serial=serial, bus_device=bus_device)
    except EM100Error as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Print device info
        print(f"MCU version: {em.mcu_version}")
        # Original hardware shows voltage in FPGA version only for newer FPGA
        if em._hwversion in (HWVERSION_EM100PRO_EARLY, HWVERSION_EM100PRO):
            if em._fpga > 0x0033:
                print(f"FPGA version: {em.fpga_version} ({em.fpga_voltage}V)")
            else:
                print(f"FPGA version: {em.fpga_version}")
        else:
            print(f"FPGA version: {em.fpga_version}")
        print(f"Hardware version: 0x{em.hwversion:02x}")
        print(f"Serial number: {em.serial_string}")
        print(f"SPI flash database: {em._chips_db.get('version', '?')}")
        print(f"EM100Pro currently {'running' if em.is_running() else 'stopped'}")
        try:
            pin = em.fpga_read(0x2A)
            print(f"EM100Pro hold pin currently {_hold_pin_name(pin)}")
        except EM100Error:
            pass
        print()

        if args.debug:
            print("Voltages:")
            em.set_led(LED_OFF)
            print(f"  1.2V:        {em.get_voltage(GET_V1_2)}mV")
            print(f"  E_VCC:       {em.get_voltage(GET_E_VCC)}mV")
            em.set_led(LED_BOTH)
            print(f"  REF+:        {em.get_voltage(GET_REF_PLUS)}mV")
            print(f"  REF-:        {em.get_voltage(GET_REF_MINUS)}mV")
            em.set_led(LED_RED)
            print(f"  Buffer VCC:  {em.get_voltage(GET_BUF_VCC)}mV")
            print(f"  Trig VCC:    {em.get_voltage(GET_TRIG_VCC)}mV")
            em.set_led(LED_BOTH)
            print(f"  RST VCC:     {em.get_voltage(GET_RST_VCC)}mV")
            print(f"  3.3V:        {em.get_voltage(GET_V3_3)}mV")
            em.set_led(LED_RED)
            print(f"  Buffer 3.3V: {em.get_voltage(GET_BUF_V3_3)}mV")
            print(f"  5V:          {em.get_voltage(GET_V5)}mV")
            em.set_led(LED_GREEN)
            print("\nFPGA registers:")
            for i in range(0, 256, 2):
                if (i % 16) == 0:
                    print(f"\n  {i:04x}: ", end="")
                try:
                    val = em.fpga_read(i)
                    print(f"{val:04x} ", end="")
                except EM100Error:
                    print("XXXX ", end="")
            print()

        # Firmware operations (mutually exclusive with other ops)
        if args.firmware_update:
            fw = pathlib.Path(args.firmware_update).read_bytes()
            em.firmware_update(fw, verify=args.verify)
            print("\nDisconnect and reconnect your EM100Pro")
            return

        if args.firmware_dump:
            print(f"\nWriting EM100Pro firmware to {args.firmware_dump}")
            data = em.firmware_dump(dpfw=False)
            pathlib.Path(args.firmware_dump).write_bytes(data)
            print("Done.")
            return

        if args.firmware_write:
            print(f"\nWriting EM100Pro firmware (DPFW) to {args.firmware_write}")
            data = em.firmware_dump(dpfw=True)
            pathlib.Path(args.firmware_write).write_bytes(data)
            print("Done.")
            return

        if args.set_serialno:
            s = args.set_serialno.strip()
            if s.upper().startswith("DP") or s.upper().startswith("EM"):
                s = s[2:]
            new_serial = int(s)
            # Read current serial page, modify, write back
            page = bytearray(em.spi_read_page(_SERIAL_ADDR))
            old_serial = struct.unpack_from("<I", page, 2)[0]
            if old_serial == new_serial:
                print("Serial number unchanged.")
            else:
                struct.pack_into("<I", page, 2, new_serial)
                if old_serial != 0xFFFFFFFF:
                    key_page = em.spi_read_page(_KEY_ADDR)
                    em.spi_unlock()
                    em.spi_id()
                    em.spi_erase_sector(0x1F)
                    em.spi_write_page(_KEY_ADDR, key_page)
                em.spi_write_page(_SERIAL_ADDR, bytes(page))
                em._read_device_info()
                print(f"New serial number: {em.serial_string}")
            return

        # Normal operations
        if args.stop:
            em.stop()
            print("Stopped EM100Pro")

        if args.set:
            em.set_chip(args.set)
            print(f"Chip set to {args.set}")
            # Auto-enable 4-byte address mode for chips >16MB unless explicit
            if not args.address_mode:
                for c in em._chips_db.get("chips", []):
                    if c["name"].lower() == args.set.lower():
                        if c["size"] > 16 * 1024 * 1024:
                            em.set_address_mode(4)
                            print("Enabled 4 byte address mode")
                        break

        if args.address_mode:
            em.set_address_mode(args.address_mode)
            print(f"Enabled {args.address_mode} byte address mode")

        if args.set_voltage:
            code = _V18 if args.set_voltage == "1.8" else _V33
            em.fpga_set_voltage(code)
            print(f"Voltage set to {args.set_voltage}")

        if args.holdpin:
            state = _HOLD_NAMES[args.holdpin.lower()]
            em.set_hold_pin(state)
            print(f"Hold pin state set to {args.holdpin.lower()}")

        if args.upload:
            chip_db = em._chips_db.get("chips", [])
            maxlen = 0x4000000  # 64 MB default
            if args.set:
                for c in chip_db:
                    if c["name"].lower() == args.set.lower():
                        maxlen = c["size"]
                        break
            print(f"Reading {maxlen} bytes from SDRAM...")
            data = em.sdram_read(0, maxlen)
            pathlib.Path(args.upload).write_bytes(data)
            print(f"Saved to {args.upload}")

        if args.download:
            data = pathlib.Path(args.download).read_bytes()
            if not data:
                print("Error: empty file", file=sys.stderr)
                sys.exit(1)
            print(f"Writing {len(data)} bytes to SDRAM...")
            em.sdram_write(0, data)
            print("Transfer succeeded")

            if args.verify:
                print("Verifying...")
                readback = em.sdram_read(0, len(data))
                if readback == data:
                    print("Verify: PASS")
                else:
                    print("Verify: FAIL")

        if args.start:
            em.start()
            print("Started EM100Pro")

        if args.trace or args.terminal:
            if not args.holdpin:
                em.set_hold_pin(HOLD_INPUT)

            if not args.start and not args.stop:
                em.start()

            addr_offset = int(args.offset, 16) if args.offset != "0" else 0

            if args.trace:
                em.trace_reset()
                print("Starting trace", end="")
            if args.terminal:
                em.ht_init()
                if args.trace:
                    print(" & ", end="")
                print("terminal", end="")
            print(". Press Ctrl-C to exit.\n")

            parser = TraceParser(addr_offset=addr_offset)
            do_exit = [False]

            def _sigint(sig, frame):
                do_exit[0] = True

            signal.signal(signal.SIGINT, _sigint)

            while not do_exit[0]:
                try:
                    if args.trace:
                        buffers = em.trace_read_raw()
                        text = parser.parse_reports(buffers)
                        if text:
                            print(text, end="", flush=True)
                        if args.terminal:
                            udata = em.ht_read_ufifo()
                            msgs = _parse_ht_messages(udata)
                            for m in msgs:
                                print(m, end="", flush=True)
                    else:
                        udata = em.ht_read_ufifo()
                        msgs = _parse_ht_messages(udata)
                        for m in msgs:
                            print(m, end="", flush=True)
                except usb.core.USBError:
                    pass

            if not args.start and not args.stop:
                em.stop()
            if args.trace:
                em.trace_reset()
            if not args.holdpin:
                em.set_hold_pin(HOLD_FLOAT)

    finally:
        em.close()


if __name__ == "__main__":
    main()
