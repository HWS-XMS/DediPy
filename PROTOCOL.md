# EM100Pro USB Protocol Reference

Reverse-engineered from sjg20/em100 (coreboot/Google) C implementation.

## Transport Layer

| Property       | Value                           |
|----------------|---------------------------------|
| USB VID        | `0x04B4` (Cypress Semiconductor)|
| USB PID        | `0x1235`                        |
| USB Speed      | 2.0 High Speed                  |
| Interface      | 0                               |
| EP1 OUT        | Bulk, 512 bytes max             |
| EP2 IN         | Bulk, 512 bytes max             |
| Timeout        | 5000 ms                         |

**Every command is exactly 16 bytes**, zero-padded, sent on EP1 OUT.
Responses are read from EP2 IN (variable length).

```
send_cmd(ep1_out, cmd[16])     -> returns bytes_sent == 16
get_response(ep2_in, buf, len) -> returns actual bytes received
```

## Command Map

### 1. System (0x10-0x13)

#### 0x10 — Get Version
```
OUT: [0x10, 0x00 x15]
IN:  [dcnt, fpga_hi, fpga_lo, mcu_hi, mcu_lo]   (5 bytes, dcnt=4)
```
- `mcu  = (mcu_hi << 8) | mcu_lo`
- `fpga = (fpga_hi << 8) | fpga_lo`
- `fpga & 0x8000` → 1.8V FPGA; else 3.3V

#### 0x11 — Set Voltage
```
OUT: [0x11, channel, mV_hi, mV_lo, 0x00 x12]
IN:  (none)
```
Channels: 0=Trig VCC, 1=RST VCC, 2=REF+, 3=REF-, 4=Buffer VCC
- Channels 0-3: `mV` value is actual millivolts (big-endian uint16)
- Channel 4 (Buffer VCC): uses **special codes**, NOT millivolts: 18=1.8V, 25=2.5V, 33=3.3V
- **Note:** When sent via chip init entries (see §10), the validation is bypassed —
  the raw 4-byte entry is sent directly via `send_cmd()`, not through `set_voltage()`.

#### 0x12 — Measure Voltage
```
OUT: [0x12, channel, 0x00 x14]
IN:  [dcnt, val_hi, val_lo]   (3 bytes, dcnt=2)
```
Channels: 0=1.2V, 1=E_VCC, 2=REF+, 3=REF-, 4=Buf VCC, 5=Trig VCC, 6=RST VCC, 7=3.3V, 8=Buf 3.3V, 9=5V
- Channels 0-3: `voltage_mV = raw * 12207 / 10000`
- Channels 4-9: `voltage_mV = raw * 48828 / 10000`

#### 0x13 — Set LED
```
OUT: [0x13, state, 0x00 x14]
IN:  (none)
```
States: 0=both off, 1=green, 2=red, 3=both on

---

### 2. FPGA (0x20-0x24)

#### 0x20 — Reconfigure FPGA
```
OUT: [0x20, 0x00 x15]
IN:  (none)
```
**Must wait 2 seconds before next command.**

#### 0x21 — Check FPGA Status
```
OUT: [0x21, 0x00 x15]
IN:  [status]   (1 byte: 1=pass, 0=fail)
```

#### 0x22 — Read FPGA Register
```
OUT: [0x22, reg_addr, 0x00 x14]
IN:  [dcnt, val_hi, val_lo]   (3 bytes, dcnt=2)
```

#### 0x23 — Write FPGA Register
```
OUT: [0x23, reg_addr, val_hi, val_lo, 0x00 x12]
IN:  (none)
```

#### 0x24 — Switch FPGA Voltage (G2 only)
```
OUT: [0x24, 0x00, 0x07, 0x80, 0x00 x12]  (for 1.8V, voltage_code=18)
OUT: [0x24, 0x00 x15]                      (for 3.3V, voltage_code=33)
IN:  (none)
```

**Full voltage switch sequence** (required order):
```
1. send cmd 0x20 (FPGA reconfigure, NO sleep here)
2. send cmd 0x24 (set voltage as above)
3. sleep(2)      (2-second mandatory delay)
4. send cmd 0x10 (get_version) → check fpga & 0x8000 for new voltage
5. verify: 0x8000 set → code 18; clear → code 33; must match requested
```

#### Key FPGA Registers
| Reg  | Function                                    |
|------|---------------------------------------------|
| 0x10 | Unknown (set to 0x00 on chip config)        |
| 0x28 | **Emulation state**: write 1=run, 0=stop    |
| 0x2a | **Hold pin state** (see protocol below)      |
| 0x32 | Set to 0xFFFF during chip init              |
| 0x38 | Chip config parameter                       |
| 0x3a | Chip size: `(val_lo + 1) * 0x10000`         |
| 0x40 | Device ID low                               |
| 0x42 | Vendor ID / Device ID high                  |
| 0x44 | Set to 0x0318 during chip init              |
| 0x46 | Chip config parameter                       |
| 0x48 | Chip config parameter                       |
| 0x81 | Unknown (set to 0x00 on chip config)        |
| 0x82 | HyperTerminal SPI command (0x11)            |
| 0xc4 | Set to 0x01 on chip config                  |

#### Hold Pin State Protocol (Register 0x2a)

Values: `0x00`=LOW, `0x02`=FLOAT, `0x03`=INPUT

**Setting** the hold pin requires a read-acknowledge-write-verify sequence:
```
1. READ  reg 0x2a → current_val
2. WRITE reg 0x2a ← (current_val | 0x04)    // acknowledge by setting bit 2
3. READ  reg 0x2a → (discard)                // re-read after ack
4. WRITE reg 0x2a ← desired_state            // 0x00, 0x02, or 0x03
5. READ  reg 0x2a → verify_val               // must equal desired_state
```
A simple write without the ack step may be silently ignored.

---

### 3. SPI Flash — Device Internal (0x30-0x37)

These operate on the EM100Pro's own 2MB SPI flash (firmware storage, serial number).

#### 0x30 — Get SPI Flash ID
```
OUT: [0x30, 0x00 x15]
IN:  [id_hi, id_mid, id_lo]   (3 bytes)
```
Expected: `0x20, 0x20, 0x15` (Micron M25P16)

#### 0x31 — Erase Entire SPI Flash
```
OUT: [0x31, 0x00 x15]
IN:  (none)
```
**Must wait 5 seconds.**

#### 0x32 — Poll SPI Flash Status
```
OUT: [0x32, 0x00 x15]
IN:  [status]   (1 byte: 1=ready, 0=busy)
```

#### 0x33 — Read SPI Flash Page (256 bytes)
```
OUT: [0x33, addr_hi, addr_mid, addr_lo, 0x00 x12]
IN:  [data x256]
```
Address is 3 bytes, big-endian (24-bit).

#### 0x34 — Write SPI Flash Page (256 bytes)
```
OUT: [0x34, addr_hi, addr_mid, addr_lo, 0x00 x12]    (command)
OUT: [data x256]                                       (data, separate bulk transfer)
```

#### 0x36 — Unlock SPI Flash
```
OUT: [0x36, 0x00 x15]
IN:  (none)
```
> The spec mentions a 5-second wait (like 0x31), but the reference implementation
> does **not** enforce it. Consider adding a delay or polling via 0x32 for safety.

#### 0x37 — Erase SPI Flash Sector (64KB)
```
OUT: [0x37, sector_num, 0x00 x14]
IN:  (none)
```
Sectors 0-31, each 64KB. Total = 2MB.
> Same 5-second wait note as 0x36 — spec says wait, implementation does not.

---

### 4. SDRAM — Emulation Memory (0x40-0x41)

These read/write the EM100Pro's SDRAM which holds the emulated flash image.
Data transfers use raw bulk transfers (not 16-byte commands) for the payload.
Chunked in 2MB (0x200000) blocks.

#### Alignment constraints (measured on EM100Pro G3, MCU 3.10 / FPGA 3.18)

- **Every bulk packet on EP1 OUT must be a full 512 bytes.** A short packet
  anywhere in the data phase (end or middle) hangs the firmware immediately:
  EP0 stops responding, the device drops off the bus on reset, and only a
  power-cycle recovers it. The device writes whole packets regardless of the
  declared length (declare 480 + send 512 → 512 bytes written, no hang).
- **Reads always return whole 512-byte packets** (request 1 → 512 bytes sent).
  A host buffer smaller than that overflows and leaves stale data in EP2 IN.
- **Addresses must be 16-byte aligned** (one burst of 8 × 16-bit words).
  Writes land at `addr & ~0xF`; reads with `addr[3:1] != 0` return each 16-byte
  block with its words permuted. Bit 0 is ignored.
- SDRAM aliases every 512 MB (`0x20000000` wraps to `0`).

The driver maps arbitrary address/length onto aligned transfers with
read-modify-write of the head/tail.

#### 0x40 — Write SDRAM
```
OUT: [0x40, addr[3:0], len[3:0], 0x00 x7]   (command, 16 bytes)
OUT: [data...]                                (bulk data, up to 2MB per transfer)
```
- Address: 4 bytes big-endian (byte 1-4)
- Length:  4 bytes big-endian (byte 5-8)

#### 0x41 — Read SDRAM
```
OUT: [0x41, addr[3:0], len[3:0], 0x00 x7]   (command, 16 bytes)
IN:  [data...]                                (bulk data, up to 2MB per transfer)
```
Same encoding as write.

---

### 5. HyperTerminal (0x50-0x53)

Debug channel: target system sends messages through SPI bus to host PC.

#### 0x50 — Read HT Register
```
OUT: [0x50, reg_addr, 0x00 x14]
IN:  [dcnt, value]   (2 bytes, dcnt=1)
```

#### 0x51 — Write HT Register
```
OUT: [0x51, reg_addr, value, 0x00 x13]
IN:  (none)
```

#### HT Registers
| Reg | Name            | R/W | Description                          |
|-----|-----------------|-----|--------------------------------------|
| 0   | status          | RW  | FIFO overflow/empty, emulation ctrl  |
| 1   | dfifo_bytes     | RO  | Valid bytes in dFIFO                 |
| 2   | ufifo_bytes     | RO  | Valid bytes in uFIFO                 |
| 3   | em100_id        | RO  | EM100 identification on SPI bus      |
| 4   | ufifo_data_fmt  | RW  | uFIFO data format                   |
| 5   | timestamp       | RO  | Timestamp                            |

Status register bits:
- Bit 0: uFIFO overflow
- Bit 3: 8-bit uFIFO byte count
- Bit 4: 1=start emulation, 0=pause
- Bit 5: uFIFO empty
- Bit 6: dFIFO empty

#### 0x52 — Write dFIFO (host → target)
```
OUT: [0x52, len_hi, len_lo, timeout_hi, timeout_lo, 0x00 x11]   (command)
OUT: [data...]                                                     (bulk transfer)
IN:  [dcnt]   (bytes successfully written)
```
Max length: 512 bytes (hardware dFIFO is 64 bytes). Timeout in ms.

> **C code bugs in `write_dfifo()`:**
> 1. Sends from a local `data[512]` buffer instead of the caller's `blk` parameter.
> 2. Response check compares `data[0] == length` but `data[0]` is `uint8` (max 255)
>    while `length` can be up to 512, so the check always fails for large writes.
> 3. Prints "Warning" on **success** (`if (bytes_sent == length)`) — condition is inverted.
>
> A reimplementation should: send the caller's data, handle the 1-byte response
> as a byte count (mod 256 or capped at 255), and not rely on the response format
> for large transfers.

#### 0x53 — Read uFIFO (target → host)
```
OUT: [0x53, len_hi, len_lo, timeout_hi, timeout_lo, 0x00 x11]   (command)
IN:  [data...]                 (first response: uFIFO data, up to `len` bytes)
IN:  [up to 2 bytes]           (second response: consumed but discarded)
```
Max length: 512 bytes. Timeout in ms.
**Two separate `get_response()` calls required.** The second read drains the
endpoint but the reference implementation discards its contents entirely.

---

### 6. SPI Trace (0xBC-0xBD)

#### 0xBC — Read SPI Trace
```
OUT: [0xBC, trace_len[3:0], timeout[3:0], trace_config, 0x00 x6]
IN:  [report_data x 8192] × trace_len   (multiple bulk reads)
```
- `trace_len`: 4 bytes big-endian, number of report buffers to read (typically 8).
  The spec says "unit is 4KB" but actual USB transfers are **8192 bytes** (8KB) each.
- `timeout`: 4 bytes big-endian, ms per report buffer
- `trace_config` (byte 9):
  - bits[1:0]: 00=follow emulation, 01=manual start, 10=trig signal
  - bit[2]: start trigger when bits[1:0]==01
  - bit[4]: set by reference implementation (0x15 = manual start + start trigger + bit 4);
    function unknown, possibly required for correct operation
  - bits[3], bits[7:5]: reserved

Each **8192-byte** report buffer:
- Bytes 0-1: record count (big-endian, max 1022)
- Then `count` records of 8 bytes each:
  - Byte 0: command ID (0xFF = timestamp record)
  - Byte 1: position counter (wraps at 8-bit boundary, increments by 0x10 per record)
  - Bytes 2-7: payload
    - Timestamp record (cmd=0xFF): 6-byte timestamp (big-endian, 48-bit)
    - Data record: byte 2 = SPI command, bytes 3-5 = address (if applicable), bytes 2-7 = data block

**Trace parsing is stateful:** The position counter, current command ID, address,
and timestamp must persist across multiple `read_spi_trace` calls. A reimplementation
needs a stateful parser (e.g. a class) to correctly decode the stream.

#### 0xBD — Reset SPI Trace
```
OUT: [0xBD, 0x00 x15]
IN:  (none)
```

---

### 7. HyperTerminal Message Format (in uFIFO)

First 2 bytes of uFIFO data = valid data length (BE).
Then messages with this structure:

```
struct em100_msg {
    uint32_t signature;    // 0x47364440
    uint8_t  data_type;
    uint8_t  data_length;
    uint8_t  data[];
};
```
Signature bytes in wire order (little-endian): `40 44 36 47` = "@D6G".
In Python: `struct.pack('<I', 0x47364440)` = `b'\x40\x44\x36\x47'`.

Data types:
| Type | Name               | Display              |
|------|--------------------|----------------------|
| 0x01 | checkpoint_1byte   | hex                  |
| 0x02 | checkpoint_2bytes  | hex                  |
| 0x03 | checkpoint_4bytes  | hex                  |
| 0x04 | hexadecimal_data   | hex                  |
| 0x05 | ascii_data         | ASCII characters     |
| 0x06 | timestamp_data     | hex                  |
| 0x07 | lookup_table       | 2-byte lookup index  |

---

### 8. Device Lifecycle

```
 1. libusb_init()
 2. open device (VID=0x04B4, PID=0x1235)
 3. if kernel driver active on interface 0 → detach it
 4. claim interface 0
 5. check_status: get_spi_flash_id() == 0x202015
 6. get_version: cmd 0x10 → mcu, fpga versions
 7. get_serialno: read_spi_flash_page(0x1FFF00) → bytes 2-5 inclusive (4 bytes, LE)
 8. ... use device ...
 9. release interface 0
10. libusb_close()
11. libusb_exit()
```

#### Terminal Init Sequence (for HyperTerminal / SPI trace terminal mode)
```
1. write_ht_register(reg=4, val=0x00)     // uFIFO data format = raw
2. write_ht_register(reg=0, val=0x10)     // status: start SPI emulation (bit 4)
3. write_fpga_register(reg=0x82, val=0x11) // set HT SPI command to 0x11
4. read_fpga_register(reg=0x28)            // read emulation state (purpose unclear)
```

### 9. Flash Memory Layout (Internal SPI)

```
0x000000 - 0x0FFFFF : FPGA firmware
0x100000 - 0x1000FF : Boot magic page (0xAA 0x55 0x42 0x4F 0x4F 0x54 0x55 0xAA)
0x100100 - 0x1EFFFF : MCU firmware
0x1F0000 - 0x1F00FF : Secret key (4 bytes + 0x00 padding)
0x1FFF00 - 0x1FFFFF : Serial number page (0xFF 0xFF serno[4] LE 0xFF padding)
```

### 10. Chip Configuration (Init Entries)

The chip database contains init arrays of **4-byte entries**. Each entry is copied
directly into bytes 0-3 of a 16-byte command buffer (rest zero-padded) and sent
via `send_cmd()`, bypassing higher-level validation.

Entry types observed:
- `[0x23, reg, val_hi, val_lo]` — write FPGA register (same as cmd 0x23)
- `[0x11, channel, mV_hi, mV_lo]` — set voltage (same as cmd 0x11, but **no validation**)

After sending all init entries, three additional FPGA registers are written:
```
write_fpga_register(0xC4, 0x01)
write_fpga_register(0x10, 0x00)
write_fpga_register(0x81, 0x00)
```

**Voltage check:** Before applying init, find the `[0x11, 0x04, mV_hi, mV_lo]` entry
(Buffer VCC) and compare against the FPGA voltage (`fpga & 0x8000` → 1.8V; else 3.3V):

| Chip voltage code | Meaning          | 1.8V FPGA | 3.3V FPGA |
|-------------------|------------------|-----------|-----------|
| 1601              | 1.65V-2.0V range | OK        | REJECT    |
| 1800              | 1.8V             | OK        | REJECT    |
| 2500              | 2.5V             | OK        | OK        |
| 3300              | 3.3V             | REJECT    | OK        |

---

### 11. DPFW Firmware File Format

```
0x00: "em100pro"          (8 bytes magic)
0x14: MCU version string  (4 bytes, e.g. "2.26")
0x1E: FPGA version string (4 bytes, e.g. "0.75")
0x28: "WFPD"              (4 bytes magic)
0x38: FPGA offset          (uint32 LE)
0x3C: FPGA length          (uint32 LE)
0x40: MCU offset           (uint32 LE)
0x44: MCU length           (uint32 LE)
0x100: [FPGA data...]
then:  [MCU data...]
```

> **C code bug:** The reference `firmware_dump()` writes `0x100 + mcu_size` to the
> MCU length field (offset 0x44) instead of just `mcu_size`. The `firmware_update()`
> reader uses this value directly, so round-tripping through the same tool works,
> but the file contains an inflated MCU length. Reimplementations should write
> the actual MCU data size.
