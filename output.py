"""Unified output backends for Roblox piano playback.

Two concrete backends share the same interface:
  * **KeyboardBackend** — emits pynput key presses (for direct keyboard mode).
  * **NumpadBackend** — emits RMC numpad protocol messages.
"""

import time
from abc import ABC, abstractmethod
from typing import Dict, Set

from pynput.keyboard import Key, Controller

from models import KeyState
from core import KeyMapper
import RobloxMidiConnect_encoder as rmc


class OutputBackend(ABC):
    """Abstract base for all output backends."""

    @abstractmethod
    def note_on(self, pitch: int, velocity: int) -> None: ...

    @abstractmethod
    def note_off(self, pitch: int) -> None: ...

    @abstractmethod
    def pedal_on(self) -> None: ...

    @abstractmethod
    def pedal_off(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release every active note and pedal.  Must be idempotent."""
        ...


# ---------------------------------------------------------------------------
# Keyboard backend
# ---------------------------------------------------------------------------

class KeyboardBackend(OutputBackend):
    """Translates note/pedal events into pynput keyboard actions."""

    def __init__(self, use_88_key_layout: bool = False):
        self._kb = Controller()
        self._mapper = KeyMapper(use_88_key_layout=use_88_key_layout)
        self._states: Dict[str, KeyState] = {}
        self._pedal_down = False

    def _state_for(self, key_char: str) -> KeyState:
        if key_char not in self._states:
            self._states[key_char] = KeyState(key_char)
        return self._states[key_char]

    # -- notes --

    def note_on(self, pitch: int, velocity: int) -> None:
        data = self._mapper.get_key_data(pitch)
        if not data:
            return
        base_key = data['key']
        modifiers = data['modifiers']
        state = self._state_for(base_key)

        was_down = state.is_physically_down
        is_sustained = state.is_sustained and not state.is_active
        state.press()

        try:
            with self._kb.pressed(*modifiers):
                if is_sustained:
                    self._kb.release(base_key)
                    time.sleep(0.001)
                    self._kb.press(base_key)
                elif not was_down:
                    self._kb.press(base_key)
        except Exception:
            pass

    def note_off(self, pitch: int) -> None:
        data = self._mapper.get_key_data(pitch)
        if not data:
            return
        base_key = data['key']
        state = self._states.get(base_key)
        if not state:
            return
        state.release()
        try:
            self._kb.release(base_key)
        except Exception:
            pass

    # -- pedal --

    def pedal_on(self) -> None:
        if not self._pedal_down:
            self._pedal_down = True
            try:
                self._kb.press(Key.space)
            except Exception:
                pass

    def pedal_off(self) -> None:
        if self._pedal_down:
            self._pedal_down = False
            try:
                self._kb.release(Key.space)
            except Exception:
                pass

    # -- shutdown --

    def shutdown(self) -> None:
        for key_char, state in self._states.items():
            if state.is_active or state.is_sustained:
                try:
                    self._kb.release(key_char)
                except Exception:
                    pass
                state.release()

        if self._pedal_down:
            self._pedal_down = False
            try:
                self._kb.release(Key.space)
            except Exception:
                pass

        for mod in (Key.shift, Key.ctrl, Key.alt):
            try:
                self._kb.release(mod)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Numpad (RMC) backend
# ---------------------------------------------------------------------------

class NumpadBackend(OutputBackend):
    """Translates note/pedal events into Roblox MIDI Connect numpad messages."""

    def __init__(self, inter_message_delay: float = 0.0):
        self._delay = inter_message_delay
        self._active_notes: Set[int] = set()
        self._pedal_down = False

    def _post_delay(self):
        if self._delay > 0:
            time.sleep(self._delay)

    # -- notes --

    def note_on(self, pitch: int, velocity: int) -> None:
        rmc.send_note_message(pitch, velocity, is_note_off=False)
        self._active_notes.add(pitch)
        self._post_delay()

    def note_off(self, pitch: int) -> None:
        rmc.send_note_message(pitch, velocity=0, is_note_off=True)
        self._active_notes.discard(pitch)
        self._post_delay()

    # -- pedal --

    def pedal_on(self) -> None:
        if not self._pedal_down:
            self._pedal_down = True
            rmc.send_pedal(127)
            self._post_delay()

    def pedal_off(self) -> None:
        if self._pedal_down:
            self._pedal_down = False
            rmc.send_pedal(0)
            self._post_delay()

    # -- shutdown --

    def shutdown(self) -> None:
        for pitch in list(self._active_notes):
            rmc.send_note_message(pitch, velocity=0, is_note_off=True)
            self._post_delay()
        self._active_notes.clear()

        if self._pedal_down:
            self._pedal_down = False
            rmc.send_pedal(0)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_backend(output_mode: str, use_88_key_layout: bool = False,
                   inter_message_delay: float = 0.0) -> OutputBackend:
    """Return the appropriate backend for *output_mode* (``'key'`` or ``'midi_numpad'``)."""
    if output_mode == 'midi_numpad':
        return NumpadBackend(inter_message_delay=inter_message_delay)
    return KeyboardBackend(use_88_key_layout=use_88_key_layout)
