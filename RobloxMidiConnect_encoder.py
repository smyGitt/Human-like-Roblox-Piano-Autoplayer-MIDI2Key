"""Roblox MIDI Connect protocol encoder.

Encodes MIDI note/velocity/pedal into sequences of numpad key presses.
Protocol: *multiply* prefix, then 4 base-12 digits via numpad keys.

The encoding tables, VK codes, and protocol constants below are defined by
the game client and **must not** be changed.
"""

import math
import time
import platform
import ctypes
from typing import Tuple


# ---------------------------------------------------------------------------
# Game-defined protocol constants  (DO NOT MODIFY)
# ---------------------------------------------------------------------------

ENCODED_KEYS = [
    "numpad0", "numpad1", "numpad2", "numpad3",
    "numpad4", "numpad5", "numpad6", "numpad7",
    "numpad8", "numpad9", "subtract", "add",
]

PEDAL_SENTINEL = 143
KEYS_PER_MESSAGE = 5  # 1 prefix + 4 data keys

# Numpad scan codes (hardware level, matching original RMC project)
_SCAN_CODES = {
    "multiply": 0x37,
    "numpad0": 0x52, "numpad1": 0x4F, "numpad2": 0x50, "numpad3": 0x51,
    "numpad4": 0x4B, "numpad5": 0x4C, "numpad6": 0x4D, "numpad7": 0x47,
    "numpad8": 0x48, "numpad9": 0x49,
    "subtract": 0x4A, "add": 0x4E,
}

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_platform = platform.system()
_numlock_ensured = False

# ---------------------------------------------------------------------------
# pydirectinput setup (scan-code based input, matching original RMC project)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Batched SendInput (scan codes) — sends one full RMC frame in 1 kernel call
# Falls back to pydirectinput per-key, then pynput per-key.
# ---------------------------------------------------------------------------

_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1

_use_batched_sendinput = False
_use_pydirectinput = False

if _platform == "Windows":
    try:
        import pydirectinput
        pydirectinput.PAUSE = 0
        for _name, _sc in _SCAN_CODES.items():
            pydirectinput.KEYBOARD_MAPPING[_name] = _sc
        _use_pydirectinput = True

        # Pre-allocate a reusable INPUT array for 5 key taps (10 events)
        # Uses pydirectinput's own ctypes structures for binary compatibility
        _frame_inputs = (pydirectinput.Input * 10)()
        _frame_extra = ctypes.c_ulong(0)
        _frame_extra_ptr = ctypes.pointer(_frame_extra)
        for _i in range(10):
            _frame_inputs[_i].type = ctypes.c_ulong(_INPUT_KEYBOARD)
            _frame_inputs[_i].ii.ki.wVk = 0
            _frame_inputs[_i].ii.ki.time = 0
            _frame_inputs[_i].ii.ki.dwExtraInfo = _frame_extra_ptr
        for _i in range(5):
            _frame_inputs[_i * 2].ii.ki.dwFlags = _KEYEVENTF_SCANCODE
            _frame_inputs[_i * 2 + 1].ii.ki.dwFlags = _KEYEVENTF_SCANCODE | _KEYEVENTF_KEYUP
        _frame_sizeof = ctypes.sizeof(pydirectinput.Input)
        _use_batched_sendinput = True
    except Exception:
        pass

# pynput fallback (also used for NumLock detection)
from pynput import keyboard as kb
_keyboard = kb.Controller()

VK_CODES = {
    "Windows": {
        "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
        "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
        "numpad8": 0x68, "numpad9": 0x69,
        "subtract": 0x6D, "add": 0x6B, "multiply": 0x6A,
    },
    "Linux": {
        "numpad0": 0xFFB0, "numpad1": 0xFFB1, "numpad2": 0xFFB2, "numpad3": 0xFFB3,
        "numpad4": 0xFFB4, "numpad5": 0xFFB5, "numpad6": 0xFFB6, "numpad7": 0xFFB7,
        "numpad8": 0xFFB8, "numpad9": 0xFFB9,
        "subtract": 0xFFAD, "add": 0xFFAB, "multiply": 0xFFAA,
    },
    "Darwin": {
        "numpad0": 0x52, "numpad1": 0x53, "numpad2": 0x54, "numpad3": 0x55,
        "numpad4": 0x56, "numpad5": 0x57, "numpad6": 0x58, "numpad7": 0x59,
        "numpad8": 0x5B, "numpad9": 0x5C,
        "subtract": 0x4E, "add": 0x45, "multiply": 0x43,
    },
}
_platform_map = VK_CODES.get(_platform, {})
_precomputed_keys = {name: kb.KeyCode.from_vk(vk) for name, vk in _platform_map.items()}

# Scan code lookup table for fast path
_ENCODED_SCAN = [_SCAN_CODES[k] for k in ENCODED_KEYS]
_MULTIPLY_SCAN = _SCAN_CODES["multiply"]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def ensure_numlock_on() -> None:
    """Activate NumLock once per session (Windows only)."""
    global _numlock_ensured
    if _numlock_ensured:
        return
    _numlock_ensured = True
    if _platform != "Windows":
        return
    try:
        if not (ctypes.windll.user32.GetKeyState(0x90) & 1):
            _keyboard.tap(kb.Key.num_lock)
    except Exception:
        pass


def _tap_key(name: str) -> None:
    """Press and release a single numpad key (pydirectinput or pynput)."""
    if _use_pydirectinput:
        try:
            pydirectinput.keyDown(name, _pause=False)
            pydirectinput.keyUp(name, _pause=False)
        except Exception:
            pass
    else:
        kc = _precomputed_keys.get(name)
        if kc is not None:
            try:
                _keyboard.press(kc)
                _keyboard.release(kc)
            except Exception:
                pass


def _send_frame_batched(sc0, sc1, sc2, sc3, sc4) -> bool:
    """Send 5 key taps as 10 INPUT events in a single SendInput call."""
    _frame_inputs[0].ii.ki.wScan = sc0
    _frame_inputs[1].ii.ki.wScan = sc0
    _frame_inputs[2].ii.ki.wScan = sc1
    _frame_inputs[3].ii.ki.wScan = sc1
    _frame_inputs[4].ii.ki.wScan = sc2
    _frame_inputs[5].ii.ki.wScan = sc2
    _frame_inputs[6].ii.ki.wScan = sc3
    _frame_inputs[7].ii.ki.wScan = sc3
    _frame_inputs[8].ii.ki.wScan = sc4
    _frame_inputs[9].ii.ki.wScan = sc4
    return ctypes.windll.user32.SendInput(10, _frame_inputs, _frame_sizeof) == 10


# ---------------------------------------------------------------------------
# Protocol message encoding / sending
# ---------------------------------------------------------------------------

def encode_and_send_message(a: int, b: int, c: int, d: int,
                            inter_key_delay: float = 0) -> None:
    """Send one RMC protocol frame: multiply prefix followed by 4 encoded digits."""
    global _use_batched_sendinput
    ensure_numlock_on()

    if _use_batched_sendinput and inter_key_delay <= 0:
        ok = _send_frame_batched(
            _MULTIPLY_SCAN,
            _ENCODED_SCAN[max(0, min(11, a))],
            _ENCODED_SCAN[max(0, min(11, b))],
            _ENCODED_SCAN[max(0, min(11, c))],
            _ENCODED_SCAN[max(0, min(11, d))],
        )
        if not ok:
            _use_batched_sendinput = False
    else:
        _tap_key("multiply")
        for val in (a, b, c, d):
            if inter_key_delay > 0:
                time.sleep(inter_key_delay)
            _tap_key(ENCODED_KEYS[max(0, min(11, val))])


def _encode_note_components(note: int, velocity: int,
                            is_note_off: bool) -> Tuple[int, int, int, int]:
    """Encode note + velocity into four base-12 values."""
    octave = math.floor(note / 12)
    note_in_octave = math.floor(note % 12)
    if is_note_off:
        return octave, note_in_octave, 0, 0
    velocity = max(0, min(127, velocity))
    return octave, note_in_octave, math.floor(velocity / 12), math.floor(velocity % 12)


def send_note_message(note: int, velocity: int, is_note_off: bool) -> None:
    """Encode a note event and send it as a single RMC message."""
    a, b, c, d = _encode_note_components(note, velocity, is_note_off)
    encode_and_send_message(a, b, c, d)


def send_pedal(value: int) -> None:
    """Encode a sustain pedal value (CC 64) and send it as a single RMC message."""
    value = max(0, min(127, value))
    a = math.floor(PEDAL_SENTINEL / 12)
    b = math.floor(PEDAL_SENTINEL % 12)
    c = math.floor(value / 12)
    d = math.floor(value % 12)
    encode_and_send_message(a, b, c, d)


def process_mido_message(msg) -> None:
    """Dispatch a *mido* MIDI message to the appropriate send function."""
    msg_type = getattr(msg, "type", None)
    if msg_type == "clock":
        return

    if msg_type in ("note_on", "note_off"):
        note = getattr(msg, "note", None)
        velocity = getattr(msg, "velocity", 0)
        if note is None:
            return
        is_off = (msg_type == "note_off"
                  or (msg_type == "note_on" and velocity == 0))
        send_note_message(note, velocity, is_off)
        return

    if msg_type == "control_change" and getattr(msg, "control", None) == 64:
        send_pedal(getattr(msg, "value", 0))
