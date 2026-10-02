# dedipy — Python driver for the Dediprog EM100Pro

Pure-Python replacement for the [sjg20/em100](https://github.com/nicerdicer/em100) C tool.
Communicates with the EM100Pro SPI flash emulator over USB using PyUSB.

## Files

| File | Purpose |
|------|---------|
| `dedipy/em100.py` | Driver class (`EM100`) + CLI |
| `dedipy/em100pro_chips.json` | Chip database (634 chips, generated from the C header) |
| `custom_chips.json` | Your custom chip definitions (optional, auto-loaded from cwd) |
| `convert_chips.py` | One-shot script to regenerate the JSON from `em100/em100pro_chips.h` |
| `PROTOCOL.md` | Reverse-engineered USB protocol reference |

## Requirements

- Python 3.8+
- [libusb](https://libusb.info/) (the native library)
- [PyUSB](https://pyusb.github.io/pyusb/) (Python bindings)

### Install

```bash
# macOS
brew install libusb

# Debian/Ubuntu
sudo apt install libusb-1.0-0-dev

# On Linux, you may need a udev rule for non-root access:
# /etc/udev/rules.d/99-em100.rules
# SUBSYSTEM=="usb", ATTR{idVendor}=="04b4", ATTR{idProduct}=="1235", MODE="0666"

# Install from local checkout
pip install .

# Or install directly from Git
pip install git+https://github.com/HWS-XMS/DediPy.git
```

After installation, the `em100` CLI command is available and the library can be
imported as `from dedipy import EM100`.

## CLI Usage

The CLI mirrors the original C tool's flags. After `pip install .`, use `em100`
directly:

```bash
# Basic workflow: stop, configure chip, load image, verify, start
em100 -s -c M25P80 -d firmware.bin -v -r

# List all supported chips
em100 --list-chips

# List connected EM100Pro devices
em100 -l

# SPI trace mode (Ctrl-C to stop)
em100 -s -c M25P80 -d firmware.bin -r -t -O fff00000

# HyperTerminal mode
em100 -T

# Combined trace + terminal
em100 -r -t -T

# Switch FPGA voltage (G2 hardware only)
em100 -V 1.8

# Set hold pin state
em100 -p INPUT

# Select device by serial number or bus:device
em100 -x DP123456 -s
em100 -x 1:23 -s

# Firmware operations
em100 -f raw_dump.bin          # dump raw 2MB flash image
em100 -g firmware.dpfw         # dump as DPFW file
em100 -F firmware.dpfw -v      # update firmware with verify

# Debug: print all voltages and FPGA registers
em100 -D
```

### All CLI Flags

| Flag | Long | Description |
|------|------|-------------|
| `-c` | `--set CHIP` | Select chip emulation |
| `-d` | `--download FILE` | Write file into SDRAM |
| `-u` | `--upload FILE` | Read SDRAM into file |
| `-r` | `--start` | Start emulation |
| `-s` | `--stop` | Stop emulation |
| `-v` | `--verify` | Verify SDRAM matches file |
| `-t` | `--trace` | SPI trace mode |
| `-O` | `--offset HEX` | Address offset for trace display |
| `-T` | `--terminal` | HyperTerminal mode |
| `-F` | `--firmware-update FILE` | Update firmware from DPFW file |
| `-f` | `--firmware-dump FILE` | Dump raw firmware to file |
| `-g` | `--firmware-write FILE` | Dump firmware as DPFW file |
| `-S` | `--set-serialno NUM` | Set device serial number |
| `-V` | `--set-voltage V` | Switch FPGA voltage (1.8 or 3.3) |
| `-p` | `--holdpin STATE` | Set hold pin (LOW, FLOAT, INPUT) |
| `-x` | `--device ID` | Select device (bus:dev or DPxxxxxx) |
| `-l` | `--list-devices` | List connected EM100Pro devices |
| `-D` | `--debug` | Print voltages and FPGA registers |
| | `--list-chips` | List all supported chip types |

## Library Usage

```python
from dedipy import EM100, EM100Error

# Open device (auto-detect)
with EM100() as dev:
    print(dev.mcu_version, dev.fpga_version, dev.fpga_voltage)

    # Configure chip and load image
    dev.set_chip("W25Q128FV")
    dev.sdram_write(0, open("firmware.bin", "rb").read())
    dev.start()

    # Read back and verify
    data = dev.sdram_read(0, 0x1000000)

    # Control
    dev.stop()
    dev.set_hold_pin(0x03)  # INPUT
    dev.set_led(1)          # green

    # Voltage
    dev.set_voltage(0, 3300)          # Trig VCC = 3.3V
    print(dev.get_voltage(7))         # measure 3.3V rail

    # SPI trace
    dev.trace_reset()
    buffers = dev.trace_read_raw()    # 8 x 8192 bytes

    # HyperTerminal
    dev.ht_init()
    ufifo_data = dev.ht_read_ufifo()

    # FPGA registers
    val = dev.fpga_read(0x28)         # emulation state
    dev.fpga_write(0x28, 1)           # start

# Open by serial number or bus address
dev = EM100(serial=123456)
dev = EM100(bus_device=(1, 23))
```

### Public API Summary

| Group | Methods |
|-------|---------|
| **Lifecycle** | `__init__(serial, bus_device)`, `close()`, context manager |
| **Properties** | `serial`, `mcu_version`, `fpga_version`, `fpga_voltage` |
| **System** | `get_version()`, `set_voltage()`, `get_voltage()`, `set_led()` |
| **FPGA** | `fpga_reconfig()`, `fpga_status()`, `fpga_read()`, `fpga_write()`, `fpga_set_voltage()` |
| **Emulation** | `start()`, `stop()`, `is_running()`, `set_hold_pin()` |
| **SPI flash** | `spi_id()`, `spi_erase()`, `spi_poll()`, `spi_read_page()`, `spi_write_page()`, `spi_unlock()`, `spi_erase_sector()` |
| **SDRAM** | `sdram_write()`, `sdram_read()` |
| **HyperTerminal** | `ht_read_reg()`, `ht_write_reg()`, `ht_write_dfifo()`, `ht_read_ufifo()`, `ht_init()` |
| **Trace** | `trace_reset()`, `trace_read_raw()` + `TraceParser` class |
| **Chip config** | `set_chip(name)`, `list_chips()` |
| **Firmware** | `firmware_dump(dpfw)`, `firmware_update(data, verify)` |

### Exceptions

- `EM100Error` — driver-level errors (device not found, verification failed, etc.)
- `usb.core.USBError` — USB transport errors (timeout, pipe error, etc.)

## Custom Chip Definitions

If your SPI flash chip isn't in the built-in database, create a `custom_chips.json`
file in your working directory. It is loaded automatically and merged with the built-in database.
Custom entries override built-in ones if the name matches.

### Quick start

1. Copy `dedipy/custom_chips.example.json` to `custom_chips.json` in your working directory
2. Find a similar built-in chip to use as a template:
   ```bash
   em100 --list-chips | grep -i winbond
   ```
3. Extract its init sequence from `dedipy/em100pro_chips.json` (search by name)
4. Edit the fields for your chip

### Format

```json
[
  {
    "vendor": "Custom",
    "name": "MY_FLASH_4MB",
    "size": 4194304,
    "init": [
      [35, 50, 255, 255],
      [35, 58,   0,  63],
      ...
    ]
  }
]
```

### Key init entries

Each entry is 4 bytes: `[command, arg1, arg2, arg3]`.

**FPGA register writes** (`command = 0x23 = 35`):

| Register | Meaning | How to set |
|----------|---------|------------|
| `0x32` | Always `0xFFFF` | `[35, 50, 255, 255]` |
| `0x3a` | Chip size | `(value + 1) * 64KB`. E.g. `63` = 4MB, `127` = 8MB, `255` = 16MB |
| `0x38` | Chip config / timing | Copy from a similar chip |
| `0x40` | Device ID bytes | From your chip's JEDEC ID |
| `0x42` | Vendor / Device ID high | From your chip's JEDEC ID |
| `0x44` | Always `0x0318` | `[35, 68, 3, 24]` |
| `0x46` | Chip config | Copy from a similar chip |
| `0x48` | Chip config | Copy from a similar chip |

**Voltage settings** (`command = 0x11 = 17`):

| Channel | Meaning | Common values |
|---------|---------|---------------|
| `2` | REF+ voltage (mV) | `[17, 2, 11, 63]` = 2879 mV |
| `3` | REF- voltage (mV) | `[17, 3, 7, 8]` = 1800 mV |
| `4` | Buffer VCC | `3300` (3.3V), `1800` (1.8V), or `2500` (2.5V) |

**Tip**: The easiest approach is to find a chip with the same voltage, similar size, and
same manufacturer in the built-in database, copy its entire init sequence, then change
only `0x3a` (size) and `0x40`/`0x42` (JEDEC IDs).

### Usage

```bash
# Your custom chip shows up alongside built-in chips
em100 --list-chips | grep -i custom

# Use it like any other chip
em100 -s -c MY_FLASH_4MB -d firmware.bin -v -r
```

## Regenerating the Chip Database

If the C source `em100/em100pro_chips.h` is updated:

```bash
python convert_chips.py
# Wrote 634 chips to dedipy/em100pro_chips.json
```

## Protocol Reference

See `PROTOCOL.md` for the complete reverse-engineered USB protocol documentation.

## Differences from the C Tool

This implementation fixes several bugs present in the C reference:

1. **dFIFO write** — sends the caller's data (C sends from an uninitialized local buffer)
2. **DPFW firmware dump** — writes correct MCU length (C writes `0x100 + mcu_size`)
3. **uFIFO read** — performs the required second USB read (protocol requirement)
4. **Trace buffers** — uses correct 8192-byte buffer size

## License

The chip database is derived from Dediprog's EM100Pro utility (v4.3.01).
The C reference implementation is Copyright 2012-2015 Google Inc. (GPL v2).
