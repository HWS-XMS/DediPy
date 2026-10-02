"""dedipy — Pure-Python driver for the Dediprog EM100Pro SPI flash emulator."""

__version__ = "1.0.0"

from .em100 import (
    EM100, EM100Error, SpiTransaction, TraceCapture, TraceParser,
    HWVERSION_EM100PRO_EARLY, HWVERSION_EM100PRO, HWVERSION_EM100PRO_G2,
)

__all__ = [
    "EM100", "EM100Error", "SpiTransaction", "TraceCapture", "TraceParser",
    "HWVERSION_EM100PRO_EARLY", "HWVERSION_EM100PRO", "HWVERSION_EM100PRO_G2",
]
