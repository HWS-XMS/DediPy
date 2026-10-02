#!/usr/bin/env python3
"""
Convert em100pro_chips.h to em100pro_chips.json.

One-shot script that parses the C chip database header from sjg20/em100
and produces a compact JSON file used by em100.py at runtime.

Requires the C source tree at em100/em100pro_chips.h (from sjg20/em100).
The JSON has already been generated and is checked in — this script is
only needed if the C chip database is updated.

Input:  em100/em100pro_chips.h  (C struct array, ~1.6 MB)
Output: em100pro_chips.json     (compact JSON, ~800 KB)

Usage:
    git clone https://github.com/nicerdicer/em100.git
    python convert_chips.py
"""

import json, re, pathlib

src = pathlib.Path(__file__).with_name("em100") / "em100pro_chips.h"
text = src.read_text()

chips = []
for m in re.finditer(
    r'\.vendor\s*=\s*"([^"]+)".*?'
    r'\.name\s*=\s*"([^"]+)".*?'
    r'\.size\s*=\s*(0x[0-9a-fA-F]+|\d+).*?'
    r'\.init\s*=\s*\{(.*?)\},\s*'
    r'\.init_len\s*=\s*(\d+)',
    text, re.DOTALL,
):
    vendor, name, size, init_block, init_len = m.groups()
    init = [
        [int(b, 16) for b in row.groups()]
        for row in re.finditer(
            r'\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,'
            r'\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}',
            init_block,
        )
    ]
    chips.append({
        "vendor": vendor, "name": name,
        "size": int(size, 16) if size.startswith("0x") else int(size),
        "init": init,
    })

out = pathlib.Path(__file__).parent / "dedipy" / "em100pro_chips.json"
out.write_text(json.dumps({"version": "4.3.01", "chips": chips}, separators=(",", ":")))
print(f"Wrote {len(chips)} chips to {out}")
