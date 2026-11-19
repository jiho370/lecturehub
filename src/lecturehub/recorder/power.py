"""Keep the PC awake while a recording runs (K6, user request 2026-09-29).

Windows may sleep during a long class (idle timer) and the capture stops. While
recording, the engine asks Windows to stay awake with SetThreadExecutionState
(ES_CONTINUOUS | ES_SYSTEM_REQUIRED, plus ES_AWAYMODE_REQUIRED where supported)
and clears it when the recording ends. Windows also clears it when the thread or
process ends, so a crash never leaves the PC awake forever. The display may still
turn off. Closing the laptop lid follows the lid setting and can still stop the
recording; the app says so.
"""
from __future__ import annotations

import os
from typing import Self

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


class KeepAwake:
    """Context manager: ``with KeepAwake() as awake: ...``; ``awake.active`` says whether Windows accepted it."""

    def __init__(self, setter=None):
        self._setter = setter
        self.active = False
        self.away_mode = False

    def _set(self, flags: int) -> int:
        if self._setter is not None:
            return self._setter(flags)
        if os.name != "nt":
            return 0
        import ctypes

        function = ctypes.windll.kernel32.SetThreadExecutionState
        function.restype = ctypes.c_uint32
        function.argtypes = [ctypes.c_uint32]
        return function(flags)

    def __enter__(self) -> Self:
        try:
            if self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED):
                self.active = self.away_mode = True
            elif self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):  # away mode not supported on this PC
                self.active = True
        except (OSError, AttributeError):
            self.active = False  # never let power management block a recording (D-050)
        return self

    def __exit__(self, *exc) -> None:
        if self.active:
            try:
                self._set(ES_CONTINUOUS)
            except (OSError, AttributeError):
                pass
            self.active = False
