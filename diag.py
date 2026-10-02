#!/usr/bin/env python3
"""Diagnostic: check EM100Pro state, trace buffer content, and SPI activity."""

from dedipy import EM100

with EM100() as dev:
    print(f"MCU: {dev.mcu_version}, FPGA: {dev.fpga_version}, Voltage: {dev.fpga_voltage}V")
    print(f"Running: {dev.is_running()}")
    print(f"Hold pin reg 0x2A: 0x{dev.fpga_read(0x2A):04x}")

    # Check a few key FPGA registers
    for reg in [0x28, 0x2A, 0x4F, 0x82, 0xC4]:
        try:
            val = dev.fpga_read(reg)
            print(f"  FPGA[0x{reg:02x}] = 0x{val:04x}")
        except Exception as e:
            print(f"  FPGA[0x{reg:02x}] = ERROR: {e}")

    # Read one round of trace buffers and inspect raw content
    print("\nReading trace buffers...")
    dev.trace_reset()
    dev.start()

    import time
    print("Emulation started. Waiting 5 seconds for SPI activity...")
    time.sleep(5)

    bufs = dev.trace_read_raw()
    dev.stop()

    for i, buf in enumerate(bufs):
        count = (buf[0] << 8) | buf[1]
        non_ff = sum(1 for b in buf if b != 0xFF)
        non_zero = sum(1 for b in buf if b != 0x00)
        print(f"  Buffer {i}: count={count}, non-FF={non_ff}/{len(buf)}, non-zero={non_zero}/{len(buf)}")
        if count > 0:
            print(f"    First 32 bytes: {buf[:32].hex()}")

    # Also check: did SDRAM get read at all?
    print("\nSDRAM first 16 bytes:", dev.sdram_read(0, 16).hex())
