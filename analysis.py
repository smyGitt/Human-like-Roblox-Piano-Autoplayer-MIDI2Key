import random
import heapq
import numpy as np
from typing import List, Set, Dict, Optional
from dataclasses import dataclass, field
from models import Note, MusicalSection, KeyEvent, Finger
from core import TempoMap, get_time_groups

class Humanizer:
    def __init__(self, config: Dict, debug_log: Optional[List[str]] = None):
        self.config = config
        self.debug_log = debug_log
        self.left_hand_drift = 0.0
        self.right_hand_drift = 0.0

    def apply_to_hand(self, notes: List[Note], hand: str, resync_points: Set[float]):
        if not any([self.config.get('vary_timing'), self.config.get('vary_articulation'), self.config.get('enable_drift_correction'), self.config.get('enable_chord_roll')]): return
        
        time_groups = get_time_groups(notes)
        for group in time_groups:
            is_resync_point = round(group[0].start_time, 2) in resync_points
            
            if self.config.get('enable_drift_correction') and is_resync_point:
                if hand == 'left': self.left_hand_drift *= self.config.get('drift_decay_factor')
                else: self.right_hand_drift *= self.config.get('drift_decay_factor')
            
            group_timing_offset = 0.0
            if self.config.get('vary_timing'):
                sigma = self.config.get('timing_variance')
                group_timing_offset = random.gauss(0, sigma)
                group_timing_offset = max(-3*sigma, min(3*sigma, group_timing_offset))

            group_articulation = self.config.get('articulation')
            if self.config.get('vary_articulation'):
                group_articulation -= (random.random() * 0.1)
                
            if self.config.get('enable_chord_roll') and len(group) > 1:
                group.sort(key=lambda n: n.pitch)
                for i, note in enumerate(group):
                    note.start_time += (i * 0.006)
                    
            for note in group:
                current_drift = self.left_hand_drift if hand == 'left' else self.right_hand_drift
                note.start_time += group_timing_offset
                if self.config.get('enable_drift_correction'):
                    note.start_time += current_drift
                
                note.duration *= group_articulation
                if note.duration < 0.03: note.duration = 0.03

            if self.config.get('enable_drift_correction'):
                if hand == 'left': self.left_hand_drift += group_timing_offset
                else: self.right_hand_drift += group_timing_offset

    def apply_tempo_rubato(self, all_notes: List[Note], sections: List[MusicalSection]):
        if not self.config.get('enable_tempo_sway'): return
        base_intensity = self.config.get('tempo_sway_intensity', 0.0)
        invert_sway = self.config.get('invert_tempo_sway', False)
        note_map = {note.id: note for note in all_notes}
        for section in sections:
            pace_multiplier = 1.0
            if section.pace_label == 'fast': pace_multiplier = 1.5 if invert_sway else 0.25
            elif section.pace_label == 'slow': pace_multiplier = 0.25 if invert_sway else 1.5
            section_duration = section.end_time - section.start_time
            if section_duration < 1.0: continue
            intensity = base_intensity * pace_multiplier
            for note in section.notes:
                if note.id in note_map:
                    rel_pos = (note.start_time - section.start_time) / section_duration
                    time_shift = np.sin(rel_pos * np.pi) * intensity
                    note_map[note.id].start_time -= time_shift

class FingeringEngine:
    MAX_HAND_SPAN = 14
    def __init__(self):
        self.fingers = [Finger(id=i, hand='left') for i in range(5)] + [Finger(id=i, hand='right') for i in range(5, 10)]

    def assign_hands(self, notes: List[Note]):
        time_groups = get_time_groups(notes)
        for group in time_groups:
            if len(group) == 1: self._assign_single_note(group[0])
            else: self._assign_chord(group)

    def _assign_single_note(self, note: Note):
        if note.hand != 'unknown': return
        note.hand = 'left' if note.pitch < 60 else 'right'

    def _assign_chord(self, chord_notes: List[Note]):
        unassigned = [n for n in chord_notes if n.hand == 'unknown']
        if not unassigned: return
        avg_pitch = sum(n.pitch for n in unassigned) / len(unassigned)
        hand = 'left' if avg_pitch < 60 else 'right'
        for n in unassigned: n.hand = hand

class SectionAnalyzer:
    def __init__(self, notes: List[Note], tempo_map: TempoMap):
        self.notes = sorted(notes, key=lambda n: n.start_time)
        self.tempo_map = tempo_map

    def analyze(self) -> List[MusicalSection]:
        if not self.notes: return []
        if self.tempo_map.has_explicit_time_signatures:
            return self._analyze_by_measures()
        else:
            return self._analyze_by_silence()

    def _analyze_by_silence(self) -> List[MusicalSection]:
        boundaries = self._detect_grand_pauses()
        sections = []
        for i in range(len(boundaries) - 1):
            start_idx = boundaries[i]
            end_idx = boundaries[i+1] - 1
            if start_idx > end_idx: continue
            sec_notes = self.notes[start_idx : end_idx+1]
            if not sec_notes: continue
            start_time = sec_notes[0].start_time
            end_time = max(n.end_time for n in sec_notes)
            start_beat = self.tempo_map.time_to_beat(start_time)
            end_beat = self.tempo_map.time_to_beat(end_time)
            articulation = self._classify_bass_articulation(sec_notes)
            pace = self._classify_pace_beats(sec_notes, start_beat, end_beat)
            sections.append(MusicalSection(start_time, end_time, sec_notes, articulation, pace, start_beat, end_beat))
        return sections

    def _analyze_by_measures(self) -> List[MusicalSection]:
        total_dur = max(n.end_time for n in self.notes)
        measures = self.tempo_map.get_measure_boundaries(total_dur)
        sections = []
        current_section_start = measures[0][0] if measures else 0
        current_notes_in_section = []
        prev_style = None
        prev_pace = None
        
        def classify_chunk(chunk_notes, s_time, e_time):
            s_beat = self.tempo_map.time_to_beat(s_time)
            e_beat = self.tempo_map.time_to_beat(e_time)
            art = self._classify_bass_articulation(chunk_notes)
            pace = self._classify_pace_beats(chunk_notes, s_beat, e_beat)
            return art, pace

        for i, (m_start, m_end) in enumerate(measures):
            notes_in_measure = [n for n in self.notes if n.start_time >= m_start and n.start_time < m_end]
            if not notes_in_measure:
                style, pace = (prev_style or 'legato'), (prev_pace or 'normal')
            else:
                style, pace = classify_chunk(notes_in_measure, m_start, m_end)
            if prev_style is None:
                prev_style = style
                prev_pace = pace
                current_notes_in_section.extend(notes_in_measure)
                continue
            if style != prev_style:
                if current_notes_in_section:
                    sec_end = m_start 
                    s_beat = self.tempo_map.time_to_beat(current_section_start)
                    e_beat = self.tempo_map.time_to_beat(sec_end)
                    sections.append(MusicalSection(current_section_start, sec_end, list(current_notes_in_section), prev_style, prev_pace, s_beat, e_beat))
                current_section_start = m_start
                current_notes_in_section = []
                prev_style = style
                prev_pace = pace
            current_notes_in_section.extend(notes_in_measure)
            
        if current_notes_in_section:
            sec_end = measures[-1][1]
            s_beat = self.tempo_map.time_to_beat(current_section_start)
            e_beat = self.tempo_map.time_to_beat(sec_end)
            sections.append(MusicalSection(current_section_start, sec_end, list(current_notes_in_section), prev_style, prev_pace, s_beat, e_beat))
        return sections

    def _detect_grand_pauses(self) -> List[int]:
        indices = [0]
        if not self.notes: return indices
        last_end_time = self.notes[0].end_time
        for i in range(1, len(self.notes)):
            current_start = self.notes[i].start_time
            gap_sec = current_start - last_end_time
            tempo = self.tempo_map.get_tempo_at(last_end_time)
            sec_per_beat = tempo / 1_000_000.0
            gap_beats = gap_sec / sec_per_beat
            if gap_beats > 2.0:
                indices.append(i)
            last_end_time = max(last_end_time, self.notes[i].end_time)
        indices.append(len(self.notes))
        return indices

    def _classify_bass_articulation(self, notes: List[Note]) -> str:
        lh_notes = [n for n in notes if n.hand == 'left']
        if len(lh_notes) < 2: return 'legato'
        total_overlap = 0.0
        total_possible = 0.0
        lh_notes.sort(key=lambda n: n.start_time)
        for i in range(len(lh_notes) - 1):
            curr = lh_notes[i]
            next_n = lh_notes[i+1]
            curr_beat = self.tempo_map.time_to_beat(curr.start_time)
            next_beat = self.tempo_map.time_to_beat(next_n.start_time)
            ioi_beats = next_beat - curr_beat
            if ioi_beats <= 0: continue
            dur_beats = self.tempo_map.time_to_beat(curr.end_time) - curr_beat
            ratio = dur_beats / ioi_beats
            total_overlap += min(ratio, 1.2)
            total_possible += 1.0
        if total_possible == 0: return 'legato'
        avg_ratio = total_overlap / total_possible
        if avg_ratio >= 0.95: return 'legato'
        if avg_ratio <= 0.60: return 'staccato'
        return 'hybrid'

    def _classify_pace_beats(self, notes: List[Note], start_beat: float, end_beat: float) -> str:
        duration_beats = end_beat - start_beat
        if duration_beats <= 0: return 'normal'
        npb = len(notes) / duration_beats
        if npb > 3.5: return 'fast'
        if npb < 1.0: return 'slow'
        return 'normal'

class PedalGenerator:
    @staticmethod
    def generate_events(config: Dict, final_notes: List[Note], sections: List[MusicalSection], debug_log: Optional[List[str]] = None) -> List[KeyEvent]:
        style = config.get('pedal_style')
        if style == 'none': return []
        events = []
        
        if style == 'hybrid':
            bass_notes = [n for n in final_notes if n.hand == 'left']
            bass_notes.sort(key=lambda n: n.start_time)
            if not bass_notes:
                treble_notes = [n for n in final_notes if n.hand == 'right']
                treble_notes.sort(key=lambda n: n.start_time)
                # Pass both the driver notes and the total track notes
                return PedalGenerator._generate_adaptive_pedal_driver(treble_notes, final_notes)
            # Pass both the driver notes and the total track notes
            return PedalGenerator._generate_adaptive_pedal_driver(bass_notes, final_notes)
            
        for section in sections:
            lh_notes = [n for n in section.notes if n.hand == 'left']
            lh_notes.sort(key=lambda n: n.start_time)
            if not lh_notes: 
                start = section.notes[0].start_time
                end = max(n.end_time for n in section.notes)
                events.append(KeyEvent(start, 1, 'pedal', 'down'))
                events.append(KeyEvent(end, 0, 'pedal', 'up'))
                continue
                
            if style == 'rhythmic':
                groups = get_time_groups(lh_notes)
                for g in groups:
                    start = g[0].start_time
                    end = max(n.end_time for n in g)
                    events.append(KeyEvent(start, 1, 'pedal', 'down'))
                    events.append(KeyEvent(end, 0, 'pedal', 'up'))
            else:
                PedalGenerator._generate_harmonic_pedal(events, lh_notes)
        return events

    @staticmethod
    def _generate_adaptive_pedal_driver(driver_notes: List[Note], all_notes: List[Note]) -> List[KeyEvent]:
        events = []
        if not driver_notes: return events
        
        PEDAL_LAG = 0.05 
        SAFE_INTERVALS = {0, 3, 4, 5, 7} # Unison, minor 3rd, Major 3rd, Perfect 4th, Perfect 5th, Octave
        UNSAFE_INTERVALS = {1, 6} # minor 2nd, Tritone

        for i in range(len(driver_notes)):
            curr = driver_notes[i]
            next_n = driver_notes[i+1] if i < len(driver_notes) - 1 else None
            
            if i == 0:
                events.append(KeyEvent(curr.start_time, 1, 'pedal', 'down'))
            
            gap = 0.0
            if next_n:
                gap = next_n.start_time - curr.end_time
            
            if gap > 0.35: 
                events.append(KeyEvent(curr.end_time, 0, 'pedal', 'up'))
                if next_n: 
                    events.append(KeyEvent(next_n.start_time, 1, 'pedal', 'down'))
            else:
                should_repedal = False
                
                if next_n:
                    # 1. Linear Harmonic Check
                    linear_interval = abs(next_n.pitch - curr.pitch) % 12
                    if linear_interval in UNSAFE_INTERVALS:
                        should_repedal = True
                    
                    # 2. Vertical Harmonic Check
                    if not should_repedal:
                        # Isolate all notes occurring within a 0.05s window of the next driver note
                        concurrent_notes = [n for n in all_notes if abs(n.start_time - next_n.start_time) <= 0.05]
                        if concurrent_notes:
                            # Establish the harmonic root for this specific timestamp
                            lowest_pitch = min(n.pitch for n in concurrent_notes)
                            
                            # Evaluate each concurrent note against the local root
                            for n in concurrent_notes:
                                vertical_interval = abs(n.pitch - lowest_pitch) % 12
                                if vertical_interval in UNSAFE_INTERVALS:
                                    should_repedal = True
                                    break # Exit early upon first detected dissonance
                
                if should_repedal and next_n:
                    events.append(KeyEvent(next_n.start_time, 0, 'pedal', 'up'))
                    events.append(KeyEvent(next_n.start_time + PEDAL_LAG, 1, 'pedal', 'down'))
                    
        final_end = max(n.end_time for n in driver_notes)
        events.append(KeyEvent(final_end, 0, 'pedal', 'up'))
        return events

    @staticmethod
    def _generate_harmonic_pedal(events: List[KeyEvent], bass_notes: List[Note]):
        if not bass_notes: return
        current_bass_pitch = -1
        for i, note in enumerate(bass_notes):
            is_new_harmony = (note.pitch != current_bass_pitch)
            prev_end = bass_notes[i-1].end_time if i > 0 else 0
            has_gap = (note.start_time - prev_end) > 0.15
            if i == 0:
                events.append(KeyEvent(note.start_time, 1, 'pedal', 'down'))
            elif has_gap:
                events.append(KeyEvent(prev_end, 0, 'pedal', 'up'))
                events.append(KeyEvent(note.start_time, 1, 'pedal', 'down'))
            elif is_new_harmony:
                events.append(KeyEvent(note.start_time, 0, 'pedal', 'up'))
                events.append(KeyEvent(note.start_time, 1, 'pedal', 'down'))
            current_bass_pitch = note.pitch
        final_end = max(n.end_time for n in bass_notes)
        events.append(KeyEvent(final_end, 0, 'pedal', 'up'))