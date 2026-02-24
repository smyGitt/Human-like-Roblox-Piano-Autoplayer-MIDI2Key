from PyQt6.QtCore import QObject, pyqtSignal as Signal
from pynput import keyboard
from pynput.keyboard import Key, Controller
import time
import threading
import heapq
import random
import bisect
import copy
from typing import List, Dict, Optional, Tuple
from models import Note, KeyEvent, MusicalSection, KeyState
from core import TempoMap, KeyMapper
from analysis import Humanizer, PedalGenerator

class Player(QObject):
    status_updated = Signal(str)
    progress_updated = Signal(float)
    playback_finished = Signal()
    visualizer_updated = Signal(list)
    auto_paused = Signal()

    def __init__(self, config: Dict, notes: List[Note], sections: List[MusicalSection], tempo_map: TempoMap):
        super().__init__()
        self.config = config
        self.notes = notes
        self.sections = sections
        self.tempo_map = tempo_map
        self.keyboard = Controller()
        self.mapper = KeyMapper(use_88_key_layout=self.config.get('use_88_key_layout', False))
        
        self.compiled_events: List[KeyEvent] = []
        self.event_index = 0
        
        self.stop_event = threading.Event()
        self.pause_event = threading.Event() 
        self.key_states: Dict[str, KeyState] = {}
        self.active_pitches: set = set()
        self.pedal_is_down = False
        
        self.start_time = 0.0
        self.total_paused_time = 0.0
        self.last_pause_timestamp = 0.0
        self.total_duration = 0.0
        
        # Throttling variables for UI updates (60 FPS)
        self.last_progress_emit_time = 0.0
        self.progress_update_interval = 1.0 / 60.0
        
        self.debug_log: Optional[List[str]] = [] if self.config.get('debug_mode') else None
        self.current_section_idx = -1
    
    def _log_debug(self, msg: str):
        if self.debug_log is not None: 
            self.debug_log.append(msg)
            self.status_updated.emit(msg)

    def play(self):
        try:
            self._log_debug("\n=== STARTING PLAYBACK PROCESS ===")
            humanized_notes = copy.deepcopy(self.notes)
            self.humanizer = Humanizer(self.config, self.debug_log)
            left_hand_notes = [n for n in humanized_notes if n.hand == 'left']
            right_hand_notes = [n for n in humanized_notes if n.hand == 'right']
            resync_points = {round(n.start_time, 2) for n in left_hand_notes}.intersection({round(n.start_time, 2) for n in right_hand_notes})
            
            self.humanizer.apply_to_hand(left_hand_notes, 'left', resync_points)
            self.humanizer.apply_to_hand(right_hand_notes, 'right', resync_points)
            
            all_notes = sorted(left_hand_notes + right_hand_notes, key=lambda n: n.start_time)
            self.humanizer.apply_tempo_rubato(all_notes, self.sections)
            
            self._compile_event_list(all_notes, self.sections)
            
            if self.config.get('countdown'): self._run_countdown()
            if self.stop_event.is_set():
                self.playback_finished.emit()
                return

            self.status_updated.emit("Playing!")
            
            self.start_time = time.perf_counter()
            self.total_paused_time = 0.0
            self.event_index = 0
            self.last_progress_emit_time = self.start_time
            
            self._run_cursor_loop()

        except Exception as e:
            import traceback
            self.status_updated.emit(f"Error: {e}\n{traceback.format_exc()}")
        finally:
            if self.stop_event.is_set(): 
                self.shutdown()
                self.playback_finished.emit()

    def stop(self):
        if not self.stop_event.is_set():
            self.status_updated.emit("Stopping playback...")
            self.stop_event.set()
            self.pause_event.clear()
            self.shutdown()

    def toggle_pause(self):
        if self.pause_event.is_set():
            if self.event_index >= len(self.compiled_events):
                 self.seek(0.0) 
            
            try:
                self.keyboard.release(Key.space)
            except: pass

            pause_duration = time.perf_counter() - self.last_pause_timestamp
            self.total_paused_time += pause_duration
            self.pause_event.clear()
            self.status_updated.emit("Resuming...")
        else:
            self.last_pause_timestamp = time.perf_counter()
            self.pause_event.set()
            self.shutdown()
            self.status_updated.emit("Paused.")

    def seek(self, target_time: float):
        self.shutdown() 
        times = [e.time for e in self.compiled_events]
        new_idx = bisect.bisect_left(times, target_time)
        self.event_index = new_idx
        
        now = time.perf_counter()
        if self.pause_event.is_set():
            self.total_paused_time = 0.0
            self.start_time = now - target_time
            self.last_pause_timestamp = now 
        else:
            self.start_time = now - target_time - self.total_paused_time
            
        self.last_progress_emit_time = now
        self.progress_updated.emit(target_time)

    def _run_countdown(self):
        self.status_updated.emit("Get ready...")
        for i in range(3, 0, -1):
            if self.stop_event.is_set(): return
            self.status_updated.emit(f"{i}...")
            time.sleep(1)

    def _compile_event_list(self, notes_to_play: List[Note], sections: List[MusicalSection]):
        self.key_states.clear()
        use_mistakes = self.config.get('enable_mistakes', False)
        mistake_chance = self.config.get('mistake_chance', 0) / 100.0
        
        temp_heap = []
        played_pitches_in_section = set()
        current_section_idx = -1
        
        for note in notes_to_play:
            note_section_idx = -1
            for i, sec in enumerate(sections):
                if sec.start_time <= note.start_time < sec.end_time:
                    note_section_idx = i; break
            
            if note_section_idx != current_section_idx:
                played_pitches_in_section.clear()
                current_section_idx = note_section_idx

            mistake_scheduled = False
            is_eligible_for_mistake = note.pitch not in played_pitches_in_section
            make_mistake = use_mistakes and is_eligible_for_mistake and (random.random() < mistake_chance)
            
            if make_mistake:
                mistake_pitch = self._get_mistake_pitch(note.pitch)
                if mistake_pitch:
                    key_data = self.mapper.get_key_data(mistake_pitch)
                    if key_data:
                        mk_char = key_data['key']
                        heapq.heappush(temp_heap, KeyEvent(note.start_time, 2, 'press', mk_char, pitch=mistake_pitch))
                        heapq.heappush(temp_heap, KeyEvent(note.start_time + note.duration, 4, 'release', mk_char, pitch=mistake_pitch))
                        mistake_scheduled = True

            if not mistake_scheduled:
                key_data = self.mapper.get_key_data(note.pitch)
                if key_data:
                    key_char = key_data['key']
                    heapq.heappush(temp_heap, KeyEvent(note.start_time, 2, 'press', key_char, pitch=note.pitch))
                    heapq.heappush(temp_heap, KeyEvent(note.end_time, 4, 'release', key_char, pitch=note.pitch))
                    if key_char not in self.key_states: self.key_states[key_char] = KeyState(key_char)
            
            played_pitches_in_section.add(note.pitch)
        
        pedal_events = PedalGenerator.generate_events(self.config, notes_to_play, sections, self.debug_log)
        for event in pedal_events: 
            heapq.heappush(temp_heap, event)
            
        self.compiled_events = []
        while temp_heap:
            self.compiled_events.append(heapq.heappop(temp_heap))
            
        self.total_duration = self.compiled_events[-1].time if self.compiled_events else 0.0
            
    def _get_mistake_pitch(self, original_pitch: int) -> Optional[int]:
        is_black = KeyMapper.is_black_key(original_pitch)
        if is_black: return original_pitch + random.choice([-2, -1, 1, 2])
        valid = [p for p in [original_pitch-2, original_pitch-1, original_pitch+1, original_pitch+2] if not KeyMapper.is_black_key(p)]
        return random.choice(valid) if valid else None

    def _run_cursor_loop(self):
        self._log_debug("\n=== ENTERING CURSOR LOOP ===")
        self.current_section_idx = -1
        
        while not self.stop_event.is_set():
            if self.pause_event.is_set():
                time.sleep(0.05)
                continue

            now = time.perf_counter()
            playback_time = (now - self.start_time) - self.total_paused_time
            
            next_sec_idx = self.current_section_idx + 1
            if next_sec_idx < len(self.sections):
                if playback_time >= self.sections[next_sec_idx].start_time:
                    self.current_section_idx = next_sec_idx
                    sec = self.sections[next_sec_idx]
                    self._log_debug(f"\n--- SECTION {next_sec_idx} | Time: {sec.start_time:.2f}s | Style: {sec.articulation_label.upper()} ---")

            if self.event_index >= len(self.compiled_events):
                if playback_time > self.total_duration + 0.1: 
                    if not self.pause_event.is_set():
                        self.last_pause_timestamp = now
                        self.pause_event.set()
                        self.shutdown()
                        self.auto_paused.emit()
                        self.status_updated.emit("Playback finished. Paused.")
                    time.sleep(0.1)
                    continue
                else:
                    time.sleep(0.001)
                    continue

            next_event = self.compiled_events[self.event_index]
            
            if next_event.time <= playback_time:
                batch = []
                while self.event_index < len(self.compiled_events):
                    e = self.compiled_events[self.event_index]
                    if e.time <= playback_time:
                        batch.append(e)
                        self.event_index += 1
                    else:
                        break
                
                batch.sort(key=lambda x: x.priority)
                self._execute_chord_event(batch, playback_time)
            else:
                time.sleep(0.001)

            # Throttle UI progress updates to ~60 FPS
            if now - self.last_progress_emit_time >= self.progress_update_interval:
                self.progress_updated.emit(playback_time)
                self.last_progress_emit_time = now

    def _get_press_info_from_event(self, event: KeyEvent) -> Tuple[List[Key], str]:
        if event.pitch is None: return [], event.key_char
        key_data = self.mapper.get_key_data(event.pitch)
        if not key_data: return [], event.key_char
        return key_data['modifiers'], key_data['key']
        
    def _execute_chord_event(self, events: List[KeyEvent], playback_time: float):
        if self.stop_event.is_set(): return
        press_events = [e for e in events if e.action == 'press']
        release_events = [e for e in events if e.action == 'release']
        pedal_events = [e for e in events if e.action == 'pedal']

        state_changed = False 

        for event in pedal_events: 
            self._log_debug(f"[ACT] {playback_time:.4f}s | PEDAL {event.key_char.upper()} (Delta: {playback_time - event.time:+.4f}s)")
            self._handle_pedal_event(event)

        for event in release_events:
            self._log_debug(f"[ACT] {playback_time:.4f}s | RELEASE | {event.key_char} (Delta: {playback_time - event.time:+.4f}s)")
            if event.pitch is not None:
                self.active_pitches.discard(event.pitch)
                state_changed = True
                
            key_char = event.key_char
            state = self.key_states.get(key_char)
            if not state: continue
            
            base_key = key_char
            if key_char in self.mapper.SYMBOL_MAP: base_key = self.mapper.SYMBOL_MAP[key_char]
            
            state.release() 
            try: 
                self.keyboard.release(base_key)
                self._log_debug(f"      [PHYSICAL] Releasing Key '{base_key}'")
            except: pass

        for event in press_events:
            self._log_debug(f"[ACT] {playback_time:.4f}s | PRESS   | {event.key_char} (Delta: {playback_time - event.time:+.4f}s)")
            if event.pitch is not None:
                self.active_pitches.add(event.pitch)
                state_changed = True
                
            state = self.key_states.get(event.key_char)
            if not state or event.pitch is None: continue
            
            modifiers, base_key = self._get_press_info_from_event(event)
            
            was_physically_down = state.is_physically_down
            is_sustained_only = state.is_sustained and not state.is_active
            state.press()
            
            try:
                with self.keyboard.pressed(*modifiers):
                    if is_sustained_only:
                        self.keyboard.release(base_key)
                        self._log_debug(f"      [PHYSICAL] Re-striking Key '{base_key}' (Sustain)")
                        time.sleep(0.001)
                        self.keyboard.press(base_key)
                    elif not was_physically_down:
                        self.keyboard.press(base_key)
                        self._log_debug(f"      [PHYSICAL] Pressing Key '{base_key}' with modifiers {modifiers}")
            except Exception: pass

        if state_changed:
            self.visualizer_updated.emit(list(self.active_pitches))

    def _handle_pedal_event(self, event: KeyEvent):
        if self.stop_event.is_set(): return
        if event.key_char == 'down' and not self.pedal_is_down:
            self.pedal_is_down = True
            try: 
                self.keyboard.press(Key.space)
                self._log_debug("      [PHYSICAL] Pressing Space (Pedal)")
            except Exception: pass
        elif event.key_char == 'up' and self.pedal_is_down:
            self.pedal_is_down = False
            try: 
                self.keyboard.release(Key.space)
                self._log_debug("      [PHYSICAL] Releasing Space (Pedal)")
            except Exception: pass

    def shutdown(self):
        self.status_updated.emit("Releasing all keys...")
        for key_char, state in self.key_states.items():
            try:
                base_key = key_char
                if key_char in self.mapper.SYMBOL_MAP: base_key = self.mapper.SYMBOL_MAP[key_char]
                if state.is_active:
                    self.keyboard.release(base_key)
                state.release()
            except Exception: pass
        
        if self.pedal_is_down:
            try: self.keyboard.release(Key.space)
            except Exception: pass
            self.pedal_is_down = False
        for key in [Key.shift, Key.ctrl, Key.alt]:
            try: self.keyboard.release(key)
            except Exception: pass
        self.status_updated.emit("Shutdown complete.")