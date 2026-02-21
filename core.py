"""MIDI parsing, tempo/beat mapping, and pitch-to-key mapping."""

import mido
import bisect
from collections import defaultdict
from typing import List, Tuple, Dict, Optional
from models import Note, MidiTrack
from pynput.keyboard import Key


def get_time_groups(notes: List[Note], threshold: float = 0.015) -> List[List[Note]]:
    """Group notes whose start times are within *threshold* seconds of each other."""
    if not notes:
        return []
    groups: List[List[Note]] = []
    current = [notes[0]]
    for i in range(1, len(notes)):
        if notes[i].start_time - current[0].start_time <= threshold:
            current.append(notes[i])
        else:
            groups.append(current)
            current = [notes[i]]
    groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Tempo / beat mapping
# ---------------------------------------------------------------------------

class TempoMap:
    """Bidirectional mapping between wall-clock seconds and musical beats.

    *tempo_events*: ``[(time_sec, tempo_microseconds), ...]``
    *time_signatures*: ``[(time_sec, numerator, denominator), ...]``
    """

    def __init__(self, tempo_events: List[Tuple[float, int]],
                 time_signatures: List[Tuple[float, int, int]]):
        self.events = sorted(tempo_events, key=lambda x: x[0])
        self.time_signatures = sorted(time_signatures, key=lambda x: x[0])
        self._segments: List[Tuple[float, float, int]] = []
        self._build_segments()
        self.has_explicit_time_signatures = (
            len(time_signatures) > 0
            and not (len(time_signatures) == 1
                     and time_signatures[0][0] == 0
                     and time_signatures[0][1] == 4)
        )

    def _build_segments(self):
        beat = 0.0
        last_t = 0.0
        tempo = 500_000
        if not self.events or self.events[0][0] > 0:
            self._segments.append((0.0, 0.0, tempo))
        for t, new_tempo in self.events:
            spb = tempo / 1_000_000.0
            beat += (t - last_t) / spb
            self._segments.append((t, beat, new_tempo))
            last_t = t
            tempo = new_tempo

    def time_to_beat(self, t: float) -> float:
        idx = bisect.bisect_right([s[0] for s in self._segments], t) - 1
        if idx < 0:
            return 0.0
        st, sb, tempo = self._segments[idx]
        return sb + (t - st) / (tempo / 1_000_000.0)

    def beat_to_time(self, b: float) -> float:
        idx = bisect.bisect_right([s[1] for s in self._segments], b) - 1
        if idx < 0:
            return 0.0
        st, sb, tempo = self._segments[idx]
        return st + (b - sb) * (tempo / 1_000_000.0)

    def get_tempo_at(self, time: float) -> int:
        idx = bisect.bisect_right([e[0] for e in self.events], time) - 1
        return self.events[idx][1] if idx >= 0 else 500_000

    def get_measure_boundaries(self, total_duration: float) -> List[Tuple[float, float]]:
        ts_list = self.time_signatures if self.time_signatures else [(0.0, 4, 4)]
        total_beats = self.time_to_beat(total_duration)
        measures: List[Tuple[float, float]] = []
        beat = 0.0
        while beat < total_beats:
            t = self.beat_to_time(beat)
            active = ts_list[0]
            for ts in ts_list:
                if ts[0] <= t + 0.001:
                    active = ts
                else:
                    break
            measure_beats = active[1] * (4.0 / active[2])
            end_beat = beat + measure_beats
            measures.append((t, self.beat_to_time(end_beat)))
            beat = end_beat
        return measures


class GlobalTickMap:
    """Converts absolute MIDI ticks to wall-clock seconds using tempo changes."""

    def __init__(self, midi_file: mido.MidiFile):
        self.ticks_per_beat = midi_file.ticks_per_beat or 480
        self._entries: List[Tuple[int, float, int]] = []
        self.time_signatures: List[Tuple[float, int, int]] = []
        self._build(midi_file)

    def _build(self, midi_file: mido.MidiFile):
        merged = mido.merge_tracks(midi_file.tracks)
        t = 0.0
        tick = 0
        tempo = 500_000
        self._entries.append((0, 0.0, tempo))
        acc = 0
        for msg in merged:
            acc += msg.time
            dt = mido.tick2second(acc - tick, self.ticks_per_beat, tempo)
            t += dt
            tick = acc
            if msg.type == 'set_tempo':
                tempo = msg.tempo
                self._entries.append((tick, t, tempo))
            elif msg.type == 'time_signature':
                self.time_signatures.append((t, msg.numerator, msg.denominator))

    def tick_to_time(self, target_tick: int) -> float:
        last_tick, last_time, tempo = self._entries[0]
        for e_tick, e_time, e_tempo in self._entries:
            if target_tick >= e_tick:
                last_tick, last_time, tempo = e_tick, e_time, e_tempo
            else:
                break
        return last_time + mido.tick2second(
            target_tick - last_tick, self.ticks_per_beat, tempo)


# ---------------------------------------------------------------------------
# MIDI file parser
# ---------------------------------------------------------------------------

class MidiParser:
    """Parse a MIDI file into :class:`MidiTrack` objects and a :class:`TempoMap`."""

    @staticmethod
    def parse_structure(filepath: str, tempo_scale: float = 1.0,
                        debug_log: Optional[List[str]] = None,
                        ) -> Tuple[List[MidiTrack], TempoMap]:
        try:
            mid = mido.MidiFile(filepath)
        except Exception as e:
            raise IOError(f"Could not read MIDI file: {e}")

        gmap = GlobalTickMap(mid)
        tempo_map = TempoMap(
            [(entry[1], entry[2]) for entry in gmap._entries],
            gmap.time_signatures,
        )

        tracks: List[MidiTrack] = []
        note_id = 0

        for i, track in enumerate(mid.tracks):
            name = f"Track {i}"
            program = 0
            is_drum = False
            notes: List[Note] = []
            open_notes: Dict[int, List[Dict]] = defaultdict(list)
            abs_tick = 0

            for msg in track:
                abs_tick += msg.time
                if msg.type == 'track_name':
                    name = msg.name
                if msg.type == 'program_change':
                    program = msg.program
                    if msg.channel == 9:
                        is_drum = True

                if msg.type == 'note_on' and msg.velocity > 0:
                    open_notes[msg.note].append(
                        {'tick': abs_tick, 'vel': msg.velocity})
                elif (msg.type == 'note_off'
                      or (msg.type == 'note_on' and msg.velocity == 0)):
                    if open_notes[msg.note]:
                        nd = open_notes[msg.note].pop(0)
                        s = gmap.tick_to_time(nd['tick'])
                        e = gmap.tick_to_time(abs_tick)
                        dur = e - s
                        if dur > 0.01:
                            notes.append(Note(
                                note_id, msg.note, nd['vel'],
                                s / tempo_scale, dur / tempo_scale,
                                'unknown', i, msg.channel,
                            ))
                            note_id += 1

            if any(n.channel == 9 for n in notes):
                is_drum = True
            if notes:
                notes.sort(key=lambda n: n.start_time)
                tracks.append(MidiTrack(i, name, program, is_drum, notes))

        return tracks, tempo_map


# ---------------------------------------------------------------------------
# Key mapping  (game-defined tables — DO NOT CHANGE the mapping constants)
# ---------------------------------------------------------------------------

class KeyMapper:
    """Map a MIDI pitch to the Roblox piano keyboard key + modifier combination.

    The mapping tables below are defined by the game client and **must not** be
    altered; doing so will cause the game to misinterpret input.
    """

    SYMBOL_MAP = {
        '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
        '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
    }

    # --- game-defined key tables ---
    LEFT_CTRL_KEYS = "1234567890qwert"
    MIDDLE_WHITE_KEYS = "1234567890qwertyuiopasdfghjklzxcvbnm"
    RIGHT_CTRL_KEYS = "yuiopasdfghj"

    PITCH_START_LEFT = 21    # A0
    PITCH_START_MIDDLE = 36  # C2
    PITCH_START_RIGHT = 97   # high range (88-key)

    def __init__(self, use_88_key_layout: bool = False):
        self.use_88_key_layout = use_88_key_layout
        self.min_pitch = 21 if use_88_key_layout else 36
        self.max_pitch = 108 if use_88_key_layout else 96
        self.key_map: Dict[int, Dict] = {}
        self._build()

    def _build(self):
        if self.use_88_key_layout:
            p = self.PITCH_START_LEFT
            for ch in self.LEFT_CTRL_KEYS:
                self.key_map[p] = {'key': ch, 'modifiers': [Key.ctrl]}
                p += 1
            p = self.PITCH_START_RIGHT
            for ch in self.RIGHT_CTRL_KEYS:
                self.key_map[p] = {'key': ch, 'modifiers': [Key.ctrl]}
                p += 1

        wi = 0
        p = self.PITCH_START_MIDDLE
        while p <= 108 and wi < len(self.MIDDLE_WHITE_KEYS):
            ch = self.MIDDLE_WHITE_KEYS[wi]
            if p not in self.key_map:
                self.key_map[p] = {'key': ch, 'modifiers': []}
            nxt = p + 1
            if self.is_black_key(nxt):
                if nxt not in self.key_map:
                    self.key_map[nxt] = {'key': ch, 'modifiers': [Key.shift]}
                p += 2
            else:
                p += 1
            wi += 1

    def get_key_data(self, pitch: int) -> Optional[Dict]:
        while pitch < self.min_pitch:
            pitch += 12
        while pitch > self.max_pitch:
            pitch -= 12
        return self.key_map.get(pitch)

    def get_key_for_pitch(self, pitch: int) -> Optional[str]:
        data = self.get_key_data(pitch)
        return data['key'] if data else None

    @staticmethod
    def is_black_key(pitch: int) -> bool:
        return (pitch % 12) in {1, 3, 6, 8, 10}

    @staticmethod
    def pitch_to_name(pitch: int) -> str:
        names = ["C", "C#", "D", "D#", "E", "F",
                 "F#", "G", "G#", "A", "A#", "B"]
        return f"{names[pitch % 12]}{(pitch // 12) - 1}"

    @property
    def lower_ctrl_bound(self):
        return 0

    @property
    def upper_ctrl_bound(self):
        return 128
