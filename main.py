#!/usr/bin/env python3
"""Jukebox GUI: MIDI file/track selection, humanization options, playback (key or Roblox MIDI Connect), hotkey, config persistence."""

import sys
import os
import re
import json
import copy
import threading
from pathlib import Path
from pynput import keyboard
from pynput.keyboard import Key, KeyCode
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QSlider, QLabel, QFileDialog,
                             QGroupBox, QTabWidget, QTextEdit, QComboBox, QDoubleSpinBox,
                             QMessageBox, QGridLayout, QStatusBar, QDialog, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView, QDialogButtonBox,
                             QSizePolicy, QScrollArea, QRadioButton)
from PyQt6.QtCore import QObject, QThread, QByteArray, pyqtSignal as Signal, Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QTextBrowser
import mido

from models import Note, MidiTrack
from core import MidiParser
from analysis import SectionAnalyzer, FingeringEngine
from visualizer import PianoWidget, TimelineWidget
from player import Player, EventCompiler
from output import create_backend

APP_NAME = "Jukebox"
APP_ID = "jukebox.piano"
APP_URL = "https://github.com/x15rte/Jukebox"
CONFIG_DIR_NAME = ".jukebox_piano"
CONFIG_FILENAME = "config.json"
LOG_FILENAME = "log.txt"


def _get_git_version() -> str:
    """Return short git rev (HEAD) for display; empty if not a repo or on error."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


APP_VERSION = _get_git_version()


def _parse_hotkey_string(s: str):
    """Parse config string to pynput Key or KeyCode (special key name or single char); default Key.f6."""
    if not s or not isinstance(s, str):
        return Key.f6
    s = s.strip().lower()
    special = getattr(Key, s, None)
    if special is not None:
        return special
    if len(s) == 1:
        try:
            return KeyCode.from_char(s)
        except Exception:
            pass
    return Key.f6


class HotkeyManager(QObject):
    """Global hotkey listener: current_key triggers toggle; start_binding() captures next key and emits bound_updated."""
    toggle_requested = Signal()
    bound_updated = Signal(str)

    def __init__(self):
        super().__init__()
        self.current_key = Key.f6
        self.listener = None
        self.listening_for_bind = False
        self._start_listener()

    def _start_listener(self):
        self.listener = keyboard.Listener(on_press=self.on_press)
        self.listener.start()

    def _format_key_string(self, key):
        if hasattr(key, 'char') and key.char:
            return key.char
        return str(key).replace('Key.', '')

    def on_press(self, key):
        if self.listening_for_bind:
            self.current_key = key
            self.listening_for_bind = False
            self.bound_updated.emit(self._format_key_string(key))
            return
        if key == self.current_key:
            self.toggle_requested.emit()

    def start_binding(self):
        self.listening_for_bind = True

class TrackSelectionDialog(QDialog):
    """Table of tracks with Play checkbox and Hand (Auto/Left/Right); get_selection() returns (track, role) for checked rows."""

    def __init__(self, tracks, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Tracks & Assign Hands")
        self.resize(700, 400)
        self.tracks = tracks
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        info_label = QLabel("Select the tracks you want to play. You can also manually assign hands to specific tracks.")
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Play", "Track Name", "Instrument", "Notes", "Hand Assignment"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        
        self.table.setRowCount(len(self.tracks))
        self.checkboxes = []
        self.role_combos = []

        for i, track in enumerate(self.tracks):
            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check_state = Qt.CheckState.Unchecked if track.is_drum else Qt.CheckState.Checked
            check_item.setCheckState(check_state)
            self.table.setItem(i, 0, check_item)
            self.checkboxes.append(check_item)
            self.table.setItem(i, 1, QTableWidgetItem(track.name))
            self.table.setItem(i, 2, QTableWidgetItem(track.instrument_name))
            self.table.setItem(i, 3, QTableWidgetItem(str(track.note_count)))
            combo = QComboBox()
            combo.addItems(["Auto-Detect", "Left Hand", "Right Hand"])
            self.table.setCellWidget(i, 4, combo)
            self.role_combos.append(combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_selection(self):
        result = []
        for i, track in enumerate(self.tracks):
            if self.checkboxes[i].checkState() == Qt.CheckState.Checked:
                role = self.role_combos[i].currentText()
                result.append((track, role))
        return result

class MidiInputWorker(QObject):
    """Reads MIDI messages from a hardware input port on a background thread."""
    message_received = Signal(object)
    connected = Signal()
    connection_error = Signal(str)
    finished = Signal()

    def __init__(self, port):
        super().__init__()
        self._port = port
        self._inport = None
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()
        port = self._inport
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def run(self):
        try:
            self._inport = mido.open_input(self._port)
        except Exception as e:
            self.connection_error.emit(str(e))
            self.finished.emit()
            return
        self.connected.emit()
        try:
            while not self._stop_event.is_set():
                for msg in self._inport.iter_pending():
                    if self._stop_event.is_set():
                        break
                    self.message_received.emit(msg)
                self._stop_event.wait(0.005)
        except Exception:
            pass
        finally:
            if self._inport is not None:
                try:
                    self._inport.close()
                except Exception:
                    pass
                self._inport = None
        self.finished.emit()


class MainWindow(QMainWindow):
    """Tabs: Playback (file, tracks, humanization), Visualizer (timeline + piano), Settings (hotkey, overlay), Output (log). Saves/loads config.json; optional log to file."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} ({APP_VERSION})" if APP_VERSION else APP_NAME)
        self.setMinimumSize(780, 520)
        self.player_thread = None
        self.player = None
        self.midi_input_thread = None
        self.midi_input_worker = None
        self.midi_input_active = False
        self.config_dir = Path.home() / CONFIG_DIR_NAME
        self.config_path = self.config_dir / CONFIG_FILENAME
        self.config_dir.mkdir(exist_ok=True)
        self.selected_tracks_info = None 
        self.parsed_tempo_map = None
        self.current_notes = [] 
        self.total_song_duration_sec = 1.0
        
        self.hotkey_manager = HotkeyManager()
        self.hotkey_manager.toggle_requested.connect(self.toggle_playback_state)
        self.hotkey_manager.bound_updated.connect(self._on_hotkey_bound)
        
        self.pedal_mapping = {
            "Original (from MIDI)": "original",
            "Automatic": "hybrid",
            "Always Sustain": "legato",
            "Rhythmic Only": "rhythmic",
            "No Pedal": "none"
        }
        self.pedal_mapping_inv = {v: k for k, v in self.pedal_mapping.items()}

        self.live_backend = None

        self._setup_ui()
        self._load_config()
        self.use_88_key_check.toggled.connect(self._on_key_layout_changed)

        ver_tag = f" ({APP_VERSION})" if APP_VERSION else ""
        self.add_log_message(f'{APP_NAME}{ver_tag} — <a href="{APP_URL}">{APP_URL}</a>')

    def _setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(10, 10, 10, 5)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        controls_tab, visual_tab, log_tab = QWidget(), QWidget(), QWidget()
        self.tabs.addTab(controls_tab, "Playback")
        self.tabs.addTab(visual_tab, "Visualizer")
        self.tabs.addTab(log_tab, "Output")

        # Visualizer tab: scrollable timeline + piano strip.
        vis_layout = QVBoxLayout(visual_tab)
        vis_layout.setContentsMargins(5, 5, 5, 5)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True) 
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.timeline_widget = TimelineWidget()
        self.timeline_widget.seek_requested.connect(self._on_timeline_seek)
        self.timeline_widget.scrub_position_changed.connect(self._on_visual_scrub)
        
        self.scroll_area.setWidget(self.timeline_widget)
        vis_layout.addWidget(self.scroll_area)
        
        self.piano_widget = PianoWidget()
        vis_layout.addWidget(self.piano_widget)

        # Playback tab: left column = Input/Output + Humanization, right column = Settings (wider, less tall).
        controls_main = QHBoxLayout(controls_tab)
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._create_input_output_group())
        self.humanization_group = self._create_humanization_group()
        left_layout.addWidget(self.humanization_group)
        left_layout.addStretch()
        controls_main.addWidget(left_column, 1)
        settings_group = self._create_settings_group()
        settings_group.setMinimumWidth(260)
        controls_main.addWidget(settings_group, 0)

        self.log_output = QTextBrowser()
        self.log_output.setOpenExternalLinks(True)
        self.log_output.setFont(QFont("Courier", 9))
        log_layout = QVBoxLayout(log_tab)
        log_layout.addWidget(self.log_output)
        
        log_btn_layout = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        copy_btn = QPushButton("Copy to Clipboard")
        self.log_save_to_file_check = QCheckBox("Save log to file")
        self.log_save_to_file_check.setChecked(False)
        self.log_save_to_file_check.toggled.connect(self._on_log_save_to_file_toggled)
        clear_btn.clicked.connect(self.log_output.clear)
        copy_btn.clicked.connect(self._copy_log_to_clipboard)
        log_btn_layout.addWidget(clear_btn)
        log_btn_layout.addWidget(copy_btn)
        log_btn_layout.addWidget(self.log_save_to_file_check)
        log_btn_layout.addStretch()
        log_layout.addLayout(log_btn_layout)

        # Bottom: time display, Play/Stop, Reset.
        media_layout = QHBoxLayout()
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        media_layout.addWidget(self.time_label)

        button_layout = QHBoxLayout()
        self.play_button = QPushButton("Play") 
        self.stop_button = QPushButton("Stop")
        button_layout.addWidget(self.play_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch()
        
        main_layout.addLayout(media_layout)
        main_layout.addLayout(button_layout)
        
        self._update_play_stop_labels()
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)

        self.play_button.clicked.connect(self.handle_play)
        self.stop_button.clicked.connect(self.handle_stop)
        self.play_button.setEnabled(False) 
        self.stop_button.setEnabled(False)

    def _toggle_always_on_top(self, checked):
        flags = self.windowFlags()
        if checked: self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else: self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def _change_opacity(self, value):
        self.setWindowOpacity(value / 100.0)

    def _change_hotkey(self):
        self.hk_btn.setText("Listening...")
        self.hk_btn.setEnabled(False)
        self.hotkey_manager.start_binding()
        QMessageBox.information(self, "Bind Key", "Press the key you want to bind now.")

    def _on_hotkey_bound(self, key_str):
        self.hk_label.setText(f"Start/Stop Hotkey: {key_str}")
        self.hk_btn.setText("Change")
        self.hk_btn.setEnabled(True)
        self._update_play_stop_labels()

    def _update_play_stop_labels(self):
        key_str = self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)
        if not self.player: self.play_button.setText(f"Play ({key_str})")
        self.stop_button.setText(f"Stop")

    def toggle_playback_state(self):
        if self.player and self.player.pause_event.is_set(): pass 
        else: self.piano_widget.clear()

        if self.player_thread and self.player_thread.isRunning():
            self.player.toggle_pause()
            self._update_pause_ui_state()
            if not self.player.pause_event.is_set():
                current_t = self.timeline_widget.current_time
                self._on_visual_scrub(current_t)
        elif self.play_button.isEnabled():
            self.handle_play()

    def _update_pause_ui_state(self):
        key_str = self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)
        if self.player and self.player.pause_event.is_set():
            self.play_button.setText(f"Resume ({key_str})")
        else:
            self.play_button.setText(f"Pause ({key_str})")

    def _on_auto_paused(self):
        self._update_pause_ui_state()
        self.piano_widget.clear()
        self.stop_button.setEnabled(True)

    def _on_timeline_seek(self, time):
        self.add_log_message(f"Seeking to {time:.2f}s...")
        if self.player: self.player.seek(time)
    
    def _on_visual_scrub(self, time):
        active_pitches = set()
        for note in self.current_notes:
            if note.start_time <= time < note.end_time: active_pitches.add(note.pitch)
        self.piano_widget.set_active_pitches(active_pitches)
        self._update_time_label(time, self.total_song_duration_sec)

    def update_progress(self, current_time):
        if self.player and self.player.total_duration > 0:
            self.total_song_duration_sec = self.player.total_duration
        if not self.timeline_widget.is_dragging:
            self.timeline_widget.set_position(current_time)
            self.piano_widget.update()
            self._update_time_label(current_time, self.total_song_duration_sec)
            timeline_width = self.timeline_widget.width()
            scroll_width = self.scroll_area.width()
            if self.total_song_duration_sec > 0:
                ratio = current_time / self.total_song_duration_sec
                cursor_x = ratio * timeline_width
                target_scroll = cursor_x - (scroll_width / 2)
                self.scroll_area.horizontalScrollBar().setValue(int(target_scroll))

    def _update_time_label(self, current, total):
        def fmt(s):
            m = int(s // 60); sec = int(s % 60)
            return f"{m:02d}:{sec:02d}"
        self.time_label.setText(f"{fmt(current)} / {fmt(total)}")

    def _copy_log_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.log_output.toPlainText())
        self.statusBar().showMessage("Log copied to clipboard!", 2000)

    def _create_info_icon(self, tooltip_text: str) -> QLabel:
        label = QLabel("\u24D8")
        label.setStyleSheet("color: gray; font-weight: bold;")
        label.setToolTip(tooltip_text)
        return label

    def _create_slider_and_spinbox(self, min_val, max_val, default_val, text_suffix="", factor=10000.0, decimals=4):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(int(min_val * factor), int(max_val * factor))
        spinbox = QDoubleSpinBox()
        spinbox.setDecimals(decimals)
        spinbox.setRange(0.0, 9999.9999)
        spinbox.setSingleStep(1.0 / factor)
        spinbox.setSuffix(text_suffix)
        slider.setValue(int(default_val * factor))
        spinbox.setValue(default_val)
        slider.valueChanged.connect(lambda v: spinbox.setValue(v / factor))
        spinbox.valueChanged.connect(lambda v: slider.setValue(int(v * factor)))
        return slider, spinbox

    def _create_input_output_group(self):
        group = QGroupBox("Input / Output")
        layout = QVBoxLayout(group)

        mode_row = QHBoxLayout()
        mode_label = QLabel("Input Mode:")
        self.input_mode_file_radio = QRadioButton("File (MIDI)")
        self.input_mode_piano_radio = QRadioButton("Piano (MIDI In)")
        self.input_mode_file_radio.setChecked(True)
        self.input_mode_file_radio.toggled.connect(self._on_input_mode_changed)
        self.input_mode_piano_radio.toggled.connect(self._on_input_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.input_mode_file_radio)
        mode_row.addWidget(self.input_mode_piano_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.file_input_widget = QWidget()
        file_layout = QVBoxLayout(self.file_input_widget)
        self.file_path_label = QLabel("No file selected.")
        self.file_path_label.setStyleSheet("font-style: italic; color: grey;")
        browse_button = QPushButton("Browse for MIDI File")
        browse_button.clicked.connect(self.select_file)
        file_layout.addWidget(self.file_path_label)
        file_layout.addWidget(browse_button)
        layout.addWidget(self.file_input_widget)

        self.piano_input_widget = QWidget()
        piano_layout = QVBoxLayout(self.piano_input_widget)
        device_row = QHBoxLayout()
        device_label = QLabel("MIDI Input Device:")
        self.midi_input_combo = QComboBox()
        self.midi_input_refresh_btn = QPushButton("Refresh")
        self.midi_input_refresh_btn.clicked.connect(self._refresh_midi_inputs)
        device_row.addWidget(device_label)
        device_row.addWidget(self.midi_input_combo)
        device_row.addWidget(self.midi_input_refresh_btn)
        piano_layout.addLayout(device_row)

        control_row = QHBoxLayout()
        self.midi_input_connect_btn = QPushButton("Connect")
        self.midi_input_disconnect_btn = QPushButton("Disconnect")
        self.midi_input_disconnect_btn.setEnabled(False)
        self.midi_input_connect_btn.clicked.connect(self._connect_midi_input)
        self.midi_input_disconnect_btn.clicked.connect(self._disconnect_midi_input)
        control_row.addWidget(self.midi_input_connect_btn)
        control_row.addWidget(self.midi_input_disconnect_btn)
        control_row.addStretch(1)
        piano_layout.addLayout(control_row)

        self.midi_input_status_label = QLabel("Piano input disabled.")
        self.midi_input_status_label.setStyleSheet("font-style: italic; color: grey;")
        piano_layout.addWidget(self.midi_input_status_label)

        layout.addWidget(self.piano_input_widget)
        self.piano_input_widget.hide()

        output_row = QHBoxLayout()
        output_label = QLabel("Output Mode:")
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("KEY Mode", userData="key")
        self.output_mode_combo.addItem("MIDI Numpad Mode", userData="midi_numpad")
        self.output_mode_combo.currentIndexChanged.connect(self._on_output_mode_changed)
        output_row.addWidget(output_label)
        output_row.addWidget(self.output_mode_combo)
        output_row.addStretch(1)
        layout.addLayout(output_row)

        self.use_88_key_check = QCheckBox("Use 88-Key Extended Layout")
        layout.addWidget(self.use_88_key_check)

        return group

    def _on_input_mode_changed(self):
        use_piano = self.input_mode_piano_radio.isChecked()
        self.file_input_widget.setVisible(not use_piano)
        self.piano_input_widget.setVisible(use_piano)
        self._playback_file_only_widget.setVisible(not use_piano)
        self.humanization_group.setVisible(not use_piano)
        # Visualizer tab is only relevant for file playback; grey out when using Piano input.
        self.tabs.setTabEnabled(1, not use_piano)
        if use_piano and self.tabs.currentIndex() == 1:
            self.tabs.setCurrentIndex(0)

        if use_piano:
            self._refresh_midi_inputs()
        else:
            if self.midi_input_active:
                self._disconnect_midi_input()

    def _refresh_midi_inputs(self):
        try:
            names = mido.get_input_names()
        except Exception as e:
            self.add_log_message(f"Failed to list MIDI input devices: {e}")
            QMessageBox.critical(self, "Error", f"Failed to list MIDI input devices:\n{e}")
            return
        self.midi_input_combo.clear()
        self.midi_input_combo.addItems(names)

    def _connect_midi_input(self):
        if self.midi_input_active:
            return

        if self.player_thread and self.player_thread.isRunning():
            self.handle_stop()

        port_name = self.midi_input_combo.currentText()
        if not port_name:
            self.add_log_message("MIDI connect skipped: no device selected.")
            QMessageBox.warning(self, "No Device", "No MIDI input device selected.")
            return

        self.live_backend = create_backend(
            self._current_output_mode(),
            self.use_88_key_check.isChecked())

        self.midi_input_thread = QThread()
        self.midi_input_worker = MidiInputWorker(port_name)
        self.midi_input_worker.moveToThread(self.midi_input_thread)
        self.midi_input_thread.started.connect(self.midi_input_worker.run)
        self.midi_input_worker.message_received.connect(self._handle_live_midi_message)
        self.midi_input_worker.connected.connect(lambda: self._on_midi_input_connected(port_name))
        self.midi_input_worker.connection_error.connect(self._on_midi_input_error)
        self.midi_input_worker.finished.connect(self.midi_input_thread.quit)
        self.midi_input_worker.finished.connect(self._on_midi_input_finished)

        self.midi_input_thread.start()
        self.midi_input_active = True
        self.midi_input_connect_btn.setEnabled(False)
        self.midi_input_disconnect_btn.setEnabled(True)
        self.midi_input_status_label.setText(f"Connecting to: {port_name}...")

    def _on_midi_input_connected(self, port_name):
        self.midi_input_status_label.setText(f"Connected to: {port_name}")
        self.add_log_message(f"Connected to MIDI input: {port_name}")

    def _on_midi_input_error(self, error_msg):
        self.add_log_message(f"MIDI input connection failed: {error_msg}")
        QMessageBox.critical(self, "Connection Failed",
                             f"Failed to open MIDI input device:\n{error_msg}")

    def _release_all_live_keys(self):
        if self.live_backend:
            self.live_backend.shutdown()

    def _disconnect_midi_input(self):
        if not self.midi_input_active:
            return
        self._release_all_live_keys()
        if self.midi_input_worker is not None:
            try:
                self.midi_input_worker.stop()
            except Exception:
                pass
        if self.midi_input_thread is not None:
            self.midi_input_thread.quit()
            self.midi_input_thread.wait(2000)

    def _on_midi_input_finished(self):
        self.midi_input_active = False
        self.midi_input_thread = None
        self.midi_input_worker = None
        self.midi_input_connect_btn.setEnabled(True)
        self.midi_input_disconnect_btn.setEnabled(False)
        self.midi_input_status_label.setText("Piano input disconnected.")
        self.add_log_message("MIDI input disconnected.")

    def _current_output_mode(self) -> str:
        if hasattr(self, "output_mode_combo"):
            data = self.output_mode_combo.currentData()
            if data:
                return data
        return "key"

    def _update_88_key_visibility(self):
        """88-key layout only applies to KEY Mode; hide the option when using MIDI Numpad."""
        if hasattr(self, "use_88_key_check"):
            self.use_88_key_check.setVisible(self._current_output_mode() == "key")

    def _on_output_mode_changed(self):
        self._update_88_key_visibility()
        if self.live_backend and self.midi_input_active:
            self.live_backend.shutdown()
            self.live_backend = create_backend(
                self._current_output_mode(),
                self.use_88_key_check.isChecked())

    def _on_key_layout_changed(self, _checked: bool = False):
        if self.live_backend and self.midi_input_active:
            self.live_backend.shutdown()
            self.live_backend = create_backend(
                self._current_output_mode(),
                self.use_88_key_check.isChecked())

    def _handle_live_midi_message(self, msg):
        if not self.live_backend:
            return

        msg_type = getattr(msg, "type", None)

        if msg_type in ("note_on", "note_off"):
            note = getattr(msg, "note", None)
            velocity = getattr(msg, "velocity", 0)
            if note is None:
                return
            is_off = (msg_type == "note_off"
                      or (msg_type == "note_on" and velocity == 0))
            if is_off:
                self.live_backend.note_off(note)
            else:
                self.live_backend.note_on(note, velocity)
            return

        if (msg_type == "control_change"
                and getattr(msg, "control", None) == 64):
            value = getattr(msg, "value", 0)
            if value >= 64:
                self.live_backend.pedal_on()
            else:
                self.live_backend.pedal_off()

    def _create_settings_group(self):
        group = QGroupBox("Settings")
        main_layout = QVBoxLayout(group)

        self._playback_file_only_widget = QWidget()
        file_grid = QGridLayout(self._playback_file_only_widget)
        tempo_label = QLabel("Tempo")
        self.tempo_slider, self.tempo_spinbox = self._create_slider_and_spinbox(10.0, 200.0, 100.0, "%", factor=10.0, decimals=1)
        file_grid.addWidget(tempo_label, 0, 0)
        file_grid.addWidget(self.tempo_slider, 0, 2)
        file_grid.addWidget(self.tempo_spinbox, 0, 3)

        pedal_label = QLabel("Pedal Style")
        self.pedal_style_combo = QComboBox()
        self.pedal_style_combo.addItems(list(self.pedal_mapping.keys()))
        self.pedal_style_combo.setItemData(0, "Uses sustain pedal data from the MIDI file. Falls back to Automatic if none found.", Qt.ItemDataRole.ToolTipRole)
        self.pedal_style_combo.setItemData(1, "Analyzes song sections to switch between Rhythmic and Sustain.", Qt.ItemDataRole.ToolTipRole)
        self.pedal_style_combo.setItemData(2, "Ignores note length. Holds pedal until harmony changes.", Qt.ItemDataRole.ToolTipRole)
        self.pedal_style_combo.setItemData(3, "Presses pedal only while keys are held down.", Qt.ItemDataRole.ToolTipRole)
        self.pedal_style_combo.setItemData(4, "Disables auto-pedal entirely.", Qt.ItemDataRole.ToolTipRole)
        file_grid.addWidget(pedal_label, 1, 0)
        file_grid.addWidget(self.pedal_style_combo, 1, 2, 1, 2)

        countdown_row = QHBoxLayout()
        self.countdown_check = QCheckBox("3 second countdown")
        countdown_row.addWidget(self.countdown_check)
        countdown_row.addStretch()
        self.reset_button = QPushButton("Reset Defaults")
        self.reset_button.clicked.connect(self._reset_controls_to_default)
        countdown_row.addWidget(self.reset_button)
        file_grid.addLayout(countdown_row, 2, 0, 1, 4)
        file_grid.setColumnStretch(2, 1)
        main_layout.addWidget(self._playback_file_only_widget)

        hk_group = QGroupBox("Hotkey")
        hk_layout = QHBoxLayout(hk_group)
        self.hk_label = QLabel(f"Start/Stop Hotkey: {self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)}")
        self.hk_btn = QPushButton("Change")
        self.hk_btn.clicked.connect(self._change_hotkey)
        hk_layout.addWidget(self.hk_label)
        hk_layout.addWidget(self.hk_btn)
        main_layout.addWidget(hk_group)

        overlay_group = QGroupBox("Overlay Mode")
        ov_layout = QGridLayout(overlay_group)
        self.always_top_check = QCheckBox("Window Always on Top")
        self.always_top_check.toggled.connect(self._toggle_always_on_top)
        opacity_label = QLabel("Window Opacity:")
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._change_opacity)
        ov_layout.addWidget(self.always_top_check, 0, 0, 1, 2)
        ov_layout.addWidget(opacity_label, 1, 0)
        ov_layout.addWidget(self.opacity_slider, 1, 1)
        main_layout.addWidget(overlay_group)

        self._reset_playback_group_to_default()
        return group

    def _create_humanization_group(self):
        group = QGroupBox("Humanization")
        main_v_layout = QVBoxLayout(group)
        self.select_all_humanization_check = QCheckBox("Select/Deselect All")
        main_v_layout.addWidget(self.select_all_humanization_check)
        self.all_humanization_checks = {}
        self.all_humanization_spinboxes = {}
        self.all_humanization_sliders = {}

        simple_toggles_layout = QHBoxLayout()
        self.all_humanization_checks['simulate_hands'] = QCheckBox("Simulate Hands")
        self.all_humanization_checks['enable_chord_roll'] = QCheckBox("Chord Rolling")
        simple_toggles_layout.addWidget(self.all_humanization_checks['simulate_hands'])
        simple_toggles_layout.addStretch(1)
        simple_toggles_layout.addWidget(self.all_humanization_checks['enable_chord_roll'])
        main_v_layout.addLayout(simple_toggles_layout)
        
        detailed_layout = QGridLayout()
        detailed_layout.setColumnStretch(2, 1) 
        
        def add_detailed_row(row_idx, name, key, min_val, max_val, def_val, suffix, factor=1.0, decimals=3):
            check = QCheckBox(name)
            slider, spinbox = self._create_slider_and_spinbox(min_val, max_val, def_val, suffix, factor=factor, decimals=decimals)
            check.toggled.connect(slider.setEnabled)
            check.toggled.connect(spinbox.setEnabled)
            detailed_layout.addWidget(check, row_idx, 0)
            detailed_layout.addWidget(slider, row_idx, 2)
            detailed_layout.addWidget(spinbox, row_idx, 3)
            self.all_humanization_checks[key] = check
            self.all_humanization_sliders[key] = slider
            self.all_humanization_spinboxes[key] = spinbox

        add_detailed_row(0, "Vary Timing", "vary_timing", 0, 0.1, 0.01, " s", factor=10000.0)
        add_detailed_row(1, "Vary Articulation", "vary_articulation", 50, 100, 95, "%", factor=100.0, decimals=1)
        add_detailed_row(2, "Hand Drift", "hand_drift", 0, 100, 25, "%", factor=100.0, decimals=1)
        add_detailed_row(3, "Mistake Chance", "mistake_chance", 0, 10, 0, "%", factor=100.0, decimals=1)
        add_detailed_row(4, "Tempo Sway", "tempo_sway", 0, 0.1, 0, " s", factor=10000.0)

        self.invert_sway_check = QCheckBox("Invert tempo sway")
        self.all_humanization_checks['invert_tempo_sway'] = self.invert_sway_check
        self.all_humanization_checks['tempo_sway'].toggled.connect(self.invert_sway_check.setEnabled)
        detailed_layout.addWidget(self.invert_sway_check, 5, 0)
        main_v_layout.addLayout(detailed_layout)
        
        self.all_humanization_checks['vary_velocity'] = QCheckBox() # Dummy for logic compatibility if needed
        self.select_all_humanization_check.toggled.connect(self._toggle_all_humanization)
        for check in self.all_humanization_checks.values():
            if check.text(): check.toggled.connect(self._update_select_all_state)
        self._reset_humanization_group_to_default()
        return group

    def _reset_controls_to_default(self):
        self.add_log_message("All settings have been reset to their default values.")
        self._reset_playback_group_to_default()
        self._reset_humanization_group_to_default()

    def _reset_playback_group_to_default(self):
        self.tempo_spinbox.setValue(100)
        self.pedal_style_combo.setCurrentText("Original (from MIDI)")
        self.use_88_key_check.setChecked(False)
        self.countdown_check.setChecked(True)

    def _reset_humanization_group_to_default(self):
        self.all_humanization_spinboxes['vary_timing'].setValue(0.010)
        self.all_humanization_spinboxes['vary_articulation'].setValue(95.0)
        self.all_humanization_spinboxes['hand_drift'].setValue(25.0)
        self.all_humanization_spinboxes['mistake_chance'].setValue(0.5)
        self.all_humanization_spinboxes['tempo_sway'].setValue(0.015)
        for check in self.all_humanization_checks.values(): 
            if check.text(): check.setChecked(False)
        self._update_enabled_states()

    def _toggle_all_humanization(self, checked):
        for check in self.all_humanization_checks.values(): 
            if check.text(): check.setChecked(checked)

    def _update_select_all_state(self):
        checks = [c for c in self.all_humanization_checks.values() if c.text()]
        is_all_checked = all(c.isChecked() for c in checks)
        self.select_all_humanization_check.blockSignals(True)
        self.select_all_humanization_check.setChecked(is_all_checked)
        self.select_all_humanization_check.blockSignals(False)

    def _get_log_file_path(self):
        return self.config_dir / LOG_FILENAME

    def _log_message_to_plain(self, message: str) -> str:
        if not message:
            return ""
        text = re.sub(r"<[^>]+>", "", message)
        try:
            import html
            text = html.unescape(text)
        except Exception:
            pass
        return text.strip()

    def _on_log_save_to_file_toggled(self, checked: bool):
        if checked:
            path = self._get_log_file_path()
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.add_log_message(f"Log is being saved to: {path}")

    def add_log_message(self, message):
        """Append to log widget; optionally append plain text to log.txt in config dir."""
        self.log_output.append(message)
        if self.log_save_to_file_check.isChecked():
            path = self._get_log_file_path()
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(self._log_message_to_plain(message) + "\n")
            except Exception:
                pass

    def set_controls_enabled(self, enabled):
        for groupbox in self.findChildren(QGroupBox): groupbox.setEnabled(enabled)

    def _save_config(self):
        """Persist UI state to config.json (humanization, hotkey, geometry, etc.)."""
        display_text = self.pedal_style_combo.currentText()
        internal_style = self.pedal_mapping.get(display_text, 'hybrid')
        config = {
            'tempo': self.tempo_spinbox.value(),
            'output_mode': self._current_output_mode(),
            'pedal_style': internal_style,
            'use_88_key_layout': self.use_88_key_check.isChecked(),
            'countdown': self.countdown_check.isChecked(),
            'select_all_humanization': self.select_all_humanization_check.isChecked(),
            'simulate_hands': self.all_humanization_checks['simulate_hands'].isChecked(),
            'enable_chord_roll': self.all_humanization_checks['enable_chord_roll'].isChecked(),
            'enable_vary_timing': self.all_humanization_checks['vary_timing'].isChecked(), 
            'value_timing_variance': self.all_humanization_spinboxes['vary_timing'].value(),
            'enable_vary_articulation': self.all_humanization_checks['vary_articulation'].isChecked(), 
            'value_articulation': self.all_humanization_spinboxes['vary_articulation'].value(),
            'enable_hand_drift': self.all_humanization_checks['hand_drift'].isChecked(), 
            'value_hand_drift_decay': self.all_humanization_spinboxes['hand_drift'].value(),
            'enable_mistakes': self.all_humanization_checks['mistake_chance'].isChecked(), 
            'value_mistake_chance': self.all_humanization_spinboxes['mistake_chance'].value(),
            'enable_tempo_sway': self.all_humanization_checks['tempo_sway'].isChecked(), 
            'value_tempo_sway_intensity': self.all_humanization_spinboxes['tempo_sway'].value(),
            'invert_tempo_sway': self.all_humanization_checks['invert_tempo_sway'].isChecked(),
            'always_on_top': self.always_top_check.isChecked(),
            'opacity': self.opacity_slider.value(),
            'hotkey': self.hotkey_manager._format_key_string(self.hotkey_manager.current_key),
            'input_mode': 'piano' if self.input_mode_piano_radio.isChecked() else 'file',
            'midi_input_device': self.midi_input_combo.currentText().strip() or None,
            'window_geometry': self.saveGeometry().toBase64().data().decode('ascii'),
            'save_log_to_file': self.log_save_to_file_check.isChecked()
        }
        try:
            with open(self.config_path, 'w') as f: json.dump(config, f, indent=4)
        except Exception as e: print(f"Error saving config: {e}")

    def _update_enabled_states(self):
        for key, check in self.all_humanization_checks.items():
            if not check.text(): continue
            is_checked = check.isChecked()
            if key in self.all_humanization_sliders: self.all_humanization_sliders[key].setEnabled(is_checked)
            if key in self.all_humanization_spinboxes: self.all_humanization_spinboxes[key].setEnabled(is_checked)
        self.invert_sway_check.setEnabled(self.all_humanization_checks['tempo_sway'].isChecked())

    def _load_config(self):
        """Restore UI from config.json; supports legacy keys (e.g. vary_timing)."""
        if not self.config_path.exists(): self._update_enabled_states(); return
        try:
            with open(self.config_path, 'r') as f: config = json.load(f)
            self.tempo_spinbox.setValue(config.get('tempo', 100.0))
            saved_output_mode = config.get('output_mode', 'key')
            for i in range(self.output_mode_combo.count()):
                if self.output_mode_combo.itemData(i) == saved_output_mode:
                    self.output_mode_combo.setCurrentIndex(i)
                    break
            self._update_88_key_visibility()
            internal_style = config.get('pedal_style', 'hybrid')
            display_text = self.pedal_mapping_inv.get(internal_style, "Original (from MIDI)")
            self.pedal_style_combo.setCurrentText(display_text)
            self.use_88_key_check.setChecked(config.get('use_88_key_layout', False))
            self.countdown_check.setChecked(config.get('countdown', True))
            self.select_all_humanization_check.setChecked(config.get('select_all_humanization', False))
            self.all_humanization_checks['simulate_hands'].setChecked(config.get('simulate_hands', False))
            self.all_humanization_checks['enable_chord_roll'].setChecked(config.get('enable_chord_roll', False))
            self.all_humanization_checks['vary_timing'].setChecked(config.get('enable_vary_timing', config.get('vary_timing', False)))
            self.all_humanization_spinboxes['vary_timing'].setValue(config.get('value_timing_variance', 0.010))
            self.all_humanization_checks['vary_articulation'].setChecked(config.get('enable_vary_articulation', False))
            self.all_humanization_spinboxes['vary_articulation'].setValue(config.get('value_articulation', 95.0))
            self.all_humanization_checks['hand_drift'].setChecked(config.get('enable_hand_drift', False))
            self.all_humanization_spinboxes['hand_drift'].setValue(config.get('value_hand_drift_decay', 25.0))
            self.all_humanization_checks['mistake_chance'].setChecked(config.get('enable_mistakes', False))
            self.all_humanization_spinboxes['mistake_chance'].setValue(config.get('value_mistake_chance', 0.5))
            self.all_humanization_checks['tempo_sway'].setChecked(config.get('enable_tempo_sway', False))
            self.all_humanization_spinboxes['tempo_sway'].setValue(config.get('value_tempo_sway_intensity', 0.015))
            self.all_humanization_checks['invert_tempo_sway'].setChecked(config.get('invert_tempo_sway', False))
            self.always_top_check.setChecked(config.get('always_on_top', False))
            self.opacity_slider.setValue(config.get('opacity', 100))
            self._change_opacity(self.opacity_slider.value())
            saved_hotkey = config.get('hotkey')
            if saved_hotkey:
                self.hotkey_manager.current_key = _parse_hotkey_string(saved_hotkey)
                self.hk_label.setText(f"Start/Stop Hotkey: {self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)}")
            input_mode = config.get('input_mode', 'file')
            self.input_mode_file_radio.setChecked(input_mode != 'piano')
            self.input_mode_piano_radio.setChecked(input_mode == 'piano')
            self._on_input_mode_changed()
            midi_device = config.get('midi_input_device')
            if midi_device:
                self.midi_input_combo.setCurrentText(midi_device)
            geom_b64 = config.get('window_geometry')
            if geom_b64:
                geom_data = QByteArray.fromBase64(geom_b64.encode('ascii'))
                if not geom_data.isEmpty():
                    self.restoreGeometry(geom_data)
            self.log_save_to_file_check.setChecked(config.get('save_log_to_file', False))
            if self.log_save_to_file_check.isChecked():
                self.add_log_message(f"Log is being saved to: {self._get_log_file_path()}")
        except Exception: self._reset_controls_to_default()
        finally: self._update_enabled_states()

    def gather_config(self):
        if not self.selected_tracks_info:
             self.add_log_message("Play aborted: no MIDI file or tracks selected.")
             QMessageBox.warning(self, "No Tracks", "Please select a MIDI file and choose tracks first."); return None
        display_text = self.pedal_style_combo.currentText()
        internal_style = self.pedal_mapping.get(display_text, 'hybrid')
        return {
            'midi_file': self.file_path_label.toolTip(), 
            'tempo': self.tempo_spinbox.value(), 
            'countdown': self.countdown_check.isChecked(),
            'use_88_key_layout': self.use_88_key_check.isChecked(),
            'pedal_style': internal_style,
            'output_mode': self._current_output_mode(),
            'simulate_hands': self.all_humanization_checks['simulate_hands'].isChecked(),
            'vary_velocity': False,
            'enable_chord_roll': self.all_humanization_checks['enable_chord_roll'].isChecked(),
            'vary_timing': self.all_humanization_checks['vary_timing'].isChecked(), 
            'timing_variance': self.all_humanization_spinboxes['vary_timing'].value(),
            'vary_articulation': self.all_humanization_checks['vary_articulation'].isChecked(), 
            'articulation': self.all_humanization_spinboxes['vary_articulation'].value() / 100.0,
            'enable_drift_correction': self.all_humanization_checks['hand_drift'].isChecked(), 
            'drift_decay_factor': self.all_humanization_spinboxes['hand_drift'].value() / 100.0,
            'enable_mistakes': self.all_humanization_checks['mistake_chance'].isChecked(), 
            'mistake_chance': self.all_humanization_spinboxes['mistake_chance'].value(),
            'enable_tempo_sway': self.all_humanization_checks['tempo_sway'].isChecked(), 
            'tempo_sway_intensity': self.all_humanization_spinboxes['tempo_sway'].value(),
            'invert_tempo_sway': self.all_humanization_checks['invert_tempo_sway'].isChecked(),
        }

    def select_file(self):
        if self.player_thread and self.player_thread.isRunning(): return
        filepath, _ = QFileDialog.getOpenFileName(self, "Select MIDI File", "", "MIDI Files (*.mid *.midi)")
        if filepath:
            self.file_path_label.setText(os.path.basename(filepath))
            self.file_path_label.setToolTip(filepath)
            self.add_log_message(f"Selected file: {filepath}")
            self._parse_and_select_tracks(filepath)

    def _parse_and_select_tracks(self, filepath):
        self.add_log_message("Parsing MIDI structure...")
        try:
            tracks, tempo_map = MidiParser.parse_structure(filepath, 1.0)
        except Exception as e:
            self.add_log_message(f"Failed to parse MIDI: {e}")
            QMessageBox.critical(self, "Error", f"Failed to parse MIDI:\n{e}")
            return
        dialog = TrackSelectionDialog(tracks, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.selected_tracks_info = dialog.get_selection()
            self.parsed_tempo_map = tempo_map 
            self.add_log_message(f"Tracks selected: {len(self.selected_tracks_info)}")
            self.play_button.setEnabled(True)

            preview_notes = []
            for track, role in self.selected_tracks_info:
                for note in track.notes:
                    n = copy.deepcopy(note)
                    if role == "Left Hand": n.hand = 'left'
                    elif role == "Right Hand": n.hand = 'right'
                    else: n.hand = 'left' if n.pitch < 60 else 'right'
                    preview_notes.append(n)
            preview_notes.sort(key=lambda n: n.start_time)
            self.current_notes = preview_notes
            total_dur = max(n.end_time for n in preview_notes) if preview_notes else 1.0
            self.total_song_duration_sec = total_dur
            self.timeline_widget.set_data(preview_notes, total_dur, tempo_map)
            self._update_time_label(0, total_dur)
            self.tabs.setCurrentIndex(1)
        else:
            self.add_log_message("Track selection cancelled.")
            self.selected_tracks_info = None
            self.play_button.setEnabled(False)

    def handle_play(self):
        if self.player_thread and self.player_thread.isRunning(): 
            self.toggle_playback_state()
            return
        config = self.gather_config()
        if not config: return
        self._save_config()
        self.add_log_message("Preparing playback...")
        tempo_scale = config['tempo'] / 100.0
        try:
             tracks, tempo_map = MidiParser.parse_structure(config['midi_file'], tempo_scale)
             selected_indices = [t.index for t, _ in self.selected_tracks_info]
             role_map = {t.index: r for t, r in self.selected_tracks_info}
             final_notes = []
             raw_pedal_events = []
             for track in tracks:
                 raw_pedal_events.extend(track.pedal_events)
                 if track.index in selected_indices:
                     role = role_map[track.index]
                     for note in track.notes:
                         new_note = copy.deepcopy(note)
                         if role == "Left Hand": new_note.hand = 'left'
                         elif role == "Right Hand": new_note.hand = 'right'
                         final_notes.append(new_note)
             raw_pedal_events.sort(key=lambda pe: pe[0])
             config['raw_pedal_events'] = raw_pedal_events
        except Exception as e:
             self.add_log_message(f"Error preparing playback: {e}")
             QMessageBox.critical(self, "Error", f"Error preparing playback:\n{e}")
             return

        final_notes.sort(key=lambda n: n.start_time)
        self.current_notes = final_notes 
        
        if config['simulate_hands']:
            self.add_log_message("Simulating hands for unassigned notes...")
            engine = FingeringEngine()
            engine.assign_hands(final_notes)
        else:
             for note in final_notes:
                 if note.hand == 'unknown':
                     note.hand = 'left' if note.pitch < 60 else 'right'

        self.add_log_message("Analyzing musical structure...")
        analyzer = SectionAnalyzer(final_notes, tempo_map)
        sections = analyzer.analyze()

        seek_ratio = 0.0
        if self.timeline_widget.total_duration > 0:
            seek_ratio = self.timeline_widget.current_time / self.timeline_widget.total_duration

        total_dur = max(n.end_time for n in final_notes) if final_notes else 1.0
        self.timeline_widget.set_data(final_notes, total_dur, tempo_map)
        self.total_song_duration_sec = total_dur

        config['start_offset'] = seek_ratio * total_dur

        self.set_controls_enabled(False)
        self.play_button.setEnabled(True) 
        self.stop_button.setEnabled(True)
        key_str = self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)
        self.play_button.setText(f"Pause ({key_str})")
        
        self.tabs.setCurrentIndex(1)
        
        backend = create_backend(config['output_mode'],
                                 config.get('use_88_key_layout', False))
        compiled_events = EventCompiler.compile(final_notes, sections, config)

        self.player_thread = QThread()
        self.player = Player(compiled_events, backend, config, total_dur)
        self.player.moveToThread(self.player_thread)
        self.player_thread.started.connect(self.player.play)
        self.player.playback_finished.connect(self.on_playback_finished)
        self.player.status_updated.connect(self.add_log_message)
        self.player.progress_updated.connect(self.update_progress)
        self.player.visualizer_updated.connect(self.piano_widget.set_pitch_active)
        self.player_thread.start()

    def handle_stop(self):
        if self.player: self.player.stop()

    def on_playback_finished(self):
        self.add_log_message("Playback process finished.\n" + "="*50 + "\n")
        self.piano_widget.clear()
        self.set_controls_enabled(True)
        self.stop_button.setEnabled(False)
        self.play_button.setText(f"Play ({self.hotkey_manager._format_key_string(self.hotkey_manager.current_key)})")
        if self.player_thread:
            self.player_thread.quit()
            self.player_thread.wait()
        self.player = None
        self.player_thread = None

    def closeEvent(self, event):
        self._save_config()
        if self.midi_input_active:
            self._disconnect_midi_input()
        if self.live_backend:
            self.live_backend.shutdown()
        if self.player and self.player_thread and self.player_thread.isRunning():
            self.player.stop()
            self.player_thread.wait(1000)
        event.accept()

if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)

    app = QApplication(sys.argv)
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())