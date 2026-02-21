"""Event compilation and timed playback engine."""

import sys
import time
import copy
import heapq

import random
import bisect
import threading
from typing import List, Dict, Set, Optional

from PyQt6.QtCore import QObject, pyqtSignal as Signal

from models import Note, KeyEvent, MusicalSection
from core import TempoMap, KeyMapper
from analysis import Humanizer, PedalGenerator
from output import OutputBackend


# ---------------------------------------------------------------------------
# Windows high-resolution timer helpers
# ---------------------------------------------------------------------------

_winmm = None
if sys.platform == "win32":
    try:
        import ctypes
        _winmm = ctypes.windll.winmm
    except Exception:
        pass


def _set_timer_resolution(ms: int = 1):
    if _winmm:
        _winmm.timeBeginPeriod(ms)


def _restore_timer_resolution(ms: int = 1):
    if _winmm:
        _winmm.timeEndPeriod(ms)


def _precise_sleep(seconds: float):
    """Busy-wait the last ~2 ms for sub-millisecond accuracy."""
    if seconds <= 0:
        return
    deadline = time.perf_counter() + seconds
    if seconds > 0.002:
        time.sleep(seconds - 0.002)
    while time.perf_counter() < deadline:
        pass


# ---------------------------------------------------------------------------
# Event compiler
# ---------------------------------------------------------------------------

class EventCompiler:
    """Compile a list of :class:`Note` objects into a time-sorted list of
    :class:`KeyEvent` objects ready for the playback engine.

    This is a stateless helper — call :meth:`compile` as a static method.
    """

    @staticmethod
    def compile(notes: List[Note], sections: List[MusicalSection],
                config: Dict) -> List[KeyEvent]:
        work = copy.deepcopy(notes)

        # --- optional humanization (delegates to analysis.py) ---
        humanize_keys = ('vary_timing', 'vary_articulation',
                         'enable_drift_correction', 'enable_chord_roll',
                         'enable_tempo_sway')
        if any(config.get(k) for k in humanize_keys):
            humanizer = Humanizer(config)
            left = [n for n in work if n.hand == 'left']
            right = [n for n in work if n.hand == 'right']
            resync = ({round(n.start_time, 2) for n in left}
                      & {round(n.start_time, 2) for n in right})
            humanizer.apply_to_hand(left, 'left', resync)
            humanizer.apply_to_hand(right, 'right', resync)
            work = sorted(left + right, key=lambda n: n.start_time)
            humanizer.apply_tempo_rubato(work, sections)

        # --- build press / release events ---
        heap: list = []
        use_mistakes = config.get('enable_mistakes', False)
        mistake_chance = config.get('mistake_chance', 0) / 100.0
        played_in_section: Set[int] = set()
        cur_sec = -1

        for note in work:
            sec_idx = -1
            for i, sec in enumerate(sections):
                if sec.start_time <= note.start_time < sec.end_time:
                    sec_idx = i
                    break
            if sec_idx != cur_sec:
                played_in_section.clear()
                cur_sec = sec_idx

            pitch = note.pitch
            did_mistake = False

            if (use_mistakes
                    and pitch not in played_in_section
                    and random.random() < mistake_chance):
                mp = EventCompiler._mistake_pitch(pitch)
                if mp is not None:
                    heapq.heappush(heap, KeyEvent(
                        note.start_time, 2, 'press', '',
                        pitch=mp, velocity=note.velocity))
                    heapq.heappush(heap, KeyEvent(
                        note.end_time, 4, 'release', '',
                        pitch=mp, velocity=0))
                    did_mistake = True

            if not did_mistake:
                heapq.heappush(heap, KeyEvent(
                    note.start_time, 2, 'press', '',
                    pitch=pitch, velocity=note.velocity))
                heapq.heappush(heap, KeyEvent(
                    note.end_time, 4, 'release', '',
                    pitch=pitch, velocity=0))

            played_in_section.add(pitch)

        # --- pedal events (from analysis.py) ---
        for pe in PedalGenerator.generate_events(config, work, sections):
            heapq.heappush(heap, pe)

        events: List[KeyEvent] = []
        while heap:
            events.append(heapq.heappop(heap))
        return events

    @staticmethod
    def _mistake_pitch(original: int) -> Optional[int]:
        if KeyMapper.is_black_key(original):
            return original + random.choice([-2, -1, 1, 2])
        candidates = [p for p in (original - 2, original - 1,
                                  original + 1, original + 2)
                      if not KeyMapper.is_black_key(p)]
        return random.choice(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Playback engine
# ---------------------------------------------------------------------------

class Player(QObject):
    """Time-accurate playback engine that dispatches compiled
    :class:`KeyEvent` objects through an :class:`OutputBackend`.

    The player knows *nothing* about the concrete output method; switching
    between keyboard and numpad mode is achieved by passing a different
    backend instance.
    """

    status_updated = Signal(str)
    progress_updated = Signal(float)
    playback_finished = Signal()
    visualizer_updated = Signal(int, bool)
    auto_paused = Signal()

    def __init__(self, compiled_events: List[KeyEvent],
                 backend: OutputBackend, config: Dict,
                 total_duration: float):
        super().__init__()
        self.events = compiled_events
        self.backend = backend
        self.config = config
        self.total_duration = total_duration

        self.event_index = 0
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

        self.start_time = 0.0
        self.total_paused_time = 0.0
        self._pause_ts = 0.0
        self._pending_shutdown = False

    # -- public API (called from main thread) --

    def play(self):
        """Entry point — run from a QThread."""
        try:
            if self.config.get('countdown'):
                self._countdown()
            if self.stop_event.is_set():
                return

            self.status_updated.emit("Playing!")
            self.start_time = time.perf_counter()
            self.total_paused_time = 0.0
            self.event_index = 0
            self._run_loop()
        except Exception as e:
            import traceback
            self.status_updated.emit(f"Error: {e}\n{traceback.format_exc()}")
        finally:
            self.backend.shutdown()
            self.playback_finished.emit()

    def stop(self):
        if not self.stop_event.is_set():
            self.status_updated.emit("Stopping playback...")
            self.stop_event.set()
            self.pause_event.clear()

    def toggle_pause(self):
        if self.pause_event.is_set():
            if self.event_index >= len(self.events):
                self.seek(0.0)
            pause_dur = time.perf_counter() - self._pause_ts
            self.total_paused_time += pause_dur
            self.pause_event.clear()
            self.status_updated.emit("Resuming...")
        else:
            self._pause_ts = time.perf_counter()
            self.pause_event.set()
            self._pending_shutdown = True
            self.status_updated.emit("Paused.")

    def seek(self, target_time: float):
        self._pending_shutdown = True
        times = [e.time for e in self.events]
        self.event_index = bisect.bisect_left(times, target_time)
        now = time.perf_counter()
        if self.pause_event.is_set():
            self.total_paused_time = 0.0
            self.start_time = now - target_time
            self._pause_ts = now
        else:
            self.start_time = now - target_time - self.total_paused_time
        self.progress_updated.emit(target_time)

    # -- internal --

    def _countdown(self):
        self.status_updated.emit("Get ready...")
        for i in range(3, 0, -1):
            if self.stop_event.is_set():
                return
            self.status_updated.emit(f"{i}...")
            time.sleep(1)

    def _run_loop(self):
        import sys
        _old_switch = sys.getswitchinterval()
        sys.setswitchinterval(0.0005)
        _set_timer_resolution(1)
        try:
            self._loop_body()
        finally:
            _restore_timer_resolution(1)
            sys.setswitchinterval(_old_switch)

    def _loop_body(self):
        last_progress = 0.0
        progress_iv = 1.0 / 30.0

        while not self.stop_event.is_set():
            if self._pending_shutdown:
                self._pending_shutdown = False
                self.backend.shutdown()

            if self.pause_event.is_set():
                time.sleep(0.05)
                continue

            now = time.perf_counter()
            pt = (now - self.start_time) - self.total_paused_time

            if self.event_index >= len(self.events):
                if pt > self.total_duration + 0.1:
                    self._pause_ts = now
                    self.pause_event.set()
                    self._pending_shutdown = True
                    self.auto_paused.emit()
                    self.status_updated.emit("Playback finished. Paused.")
                    time.sleep(0.1)
                    continue
                time.sleep(0.005)
                continue

            nxt = self.events[self.event_index]
            wait = nxt.time - pt

            if wait > 0:
                _precise_sleep(wait)

            batch: List[KeyEvent] = []
            batch_time = nxt.time
            while self.event_index < len(self.events):
                e = self.events[self.event_index]
                if e.time <= batch_time + 0.0005:
                    batch.append(e)
                    self.event_index += 1
                else:
                    break

            if batch:
                self._execute_batch(batch)

            now = time.perf_counter()
            pt = (now - self.start_time) - self.total_paused_time
            if now - last_progress >= progress_iv:
                self.progress_updated.emit(pt)
                last_progress = now

    def _execute_batch(self, events: List[KeyEvent]):
        """Execute a batch of events that fall within the same time-slice.

        Order within a batch: pedal → release → press.
        """
        if self.stop_event.is_set():
            return

        pedals = [e for e in events if e.action == 'pedal']
        releases = [e for e in events if e.action == 'release']
        presses = [e for e in events if e.action == 'press']

        for e in pedals:
            if e.key_char == 'down':
                self.backend.pedal_on()
            else:
                self.backend.pedal_off()

        for e in releases:
            if self.stop_event.is_set():
                return
            if e.pitch is not None:
                self.backend.note_off(e.pitch)
                self.visualizer_updated.emit(e.pitch, False)

        for e in presses:
            if self.stop_event.is_set():
                return
            if e.pitch is not None:
                self.backend.note_on(e.pitch, e.velocity)
                self.visualizer_updated.emit(e.pitch, True)
