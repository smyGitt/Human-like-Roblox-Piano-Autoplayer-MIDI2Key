from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal as Signal
from PyQt6.QtGui import QPainter, QBrush, QColor, QPen, QPixmap
from typing import List, Set
from models import Note
from core import TempoMap

class PianoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(80)
        self.setMinimumWidth(500)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.active_pitches = set()
        self.min_pitch = 21 
        self.max_pitch = 108 
        self.white_keys_count = 52 
        self.black_keys = {1, 3, 6, 8, 10} 

    def set_active_pitches(self, pitches: list):
        self.active_pitches = set(pitches)
        self.update()
        
    def clear(self):
        self.active_pitches.clear()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        width = self.width()
        height = self.height()
        key_width = width / self.white_keys_count
        black_key_width = key_width * 0.65
        black_key_height = height * 0.6
        
        white_brush = QBrush(QColor(255, 255, 255))
        black_brush = QBrush(QColor(0, 0, 0))
        active_brush = QBrush(QColor(0, 255, 100)) 
        
        white_idx = 0
        white_key_rects = {} 
        
        for p in range(self.min_pitch, self.max_pitch + 1):
            if (p % 12) in self.black_keys: continue
            x = white_idx * key_width
            rect = QRectF(x, 0, key_width, height)
            white_key_rects[p] = rect
            
            brush = active_brush if p in self.active_pitches else white_brush
            painter.setBrush(brush)
            painter.setPen(QPen(QColor(0,0,0), 1))
            painter.drawRect(rect)
            white_idx += 1

        for p in range(self.min_pitch, self.max_pitch + 1):
            if (p % 12) not in self.black_keys: continue
            prev_white = p - 1
            if prev_white not in white_key_rects: continue
                
            ref_rect = white_key_rects[prev_white]
            x = ref_rect.right() - (black_key_width / 2)
            rect = QRectF(x, 0, black_key_width, black_key_height)
            
            brush = active_brush if p in self.active_pitches else black_brush
            painter.setBrush(brush)
            painter.setPen(QPen(QColor(0,0,0), 1))
            painter.drawRect(rect)


class TimelineWidget(QWidget):
    seek_requested = Signal(float)
    scrub_position_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.notes = []
        self.total_duration = 1.0
        self.current_time = 0.0
        self.is_dragging = False
        self.pixels_per_second = 50 
        self.tempo_map = None 
        
        self.cached_background = None
        
        self.bg_color = QColor(30, 30, 30)
        self.left_hand_color = QColor(80, 160, 255, 200) 
        self.right_hand_color = QColor(255, 80, 80, 200) 
        self.unknown_color = QColor(150, 150, 150, 150)
        self.cursor_color = QColor(255, 255, 255)
        self.measure_line_color = QColor(255, 255, 255, 50)
        
    def set_data(self, notes: List[Note], duration: float, tempo_map: TempoMap = None):
        self.notes = notes
        self.total_duration = max(duration, 0.1)
        self.tempo_map = tempo_map
        
        new_width = int(self.total_duration * self.pixels_per_second)
        new_width = max(new_width, 800)
        new_width = min(new_width, 16384)
        self.setFixedWidth(new_width)
        
        self.cached_background = None
        self.update()

    def set_position(self, time: float):
        if not self.is_dragging:
            self.current_time = time
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self._handle_mouse_input(event.position().x())

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self._handle_mouse_input(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = False
            self.seek_requested.emit(self.current_time)

    def _handle_mouse_input(self, x):
        ratio = max(0.0, min(1.0, x / self.width()))
        self.current_time = ratio * self.total_duration
        self.scrub_position_changed.emit(self.current_time)
        self.update()

    def resizeEvent(self, event):
        self.cached_background = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        w = self.width()
        h = self.height()

        if self.cached_background is None or self.cached_background.size() != self.size():
            self.cached_background = QPixmap(self.size())
            self.cached_background.fill(self.bg_color)
            
            cache_painter = QPainter(self.cached_background)
            cache_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            if self.tempo_map:
                cache_painter.setPen(QPen(self.measure_line_color, 1))
                try:
                    boundaries = self.tempo_map.get_measure_boundaries(self.total_duration)
                    for start_t, end_t in boundaries:
                        x = (start_t / self.total_duration) * w
                        cache_painter.drawLine(QPointF(x, 0), QPointF(x, h))
                except Exception: pass

            if self.notes:
                min_p = 21
                max_p = 108
                range_p = max_p - min_p
                
                cache_painter.setPen(Qt.PenStyle.NoPen)
                
                for note in self.notes:
                    nx = (note.start_time / self.total_duration) * w
                    nw = (note.duration / self.total_duration) * w
                    nw = max(1.0, nw)
                    
                    ny_ratio = 1.0 - ((note.pitch - min_p) / range_p)
                    ny = ny_ratio * (h - 10) + 5
                    nh = 8 
                    
                    if note.hand == 'left':
                        cache_painter.setBrush(QBrush(self.left_hand_color))
                    elif note.hand == 'right':
                        cache_painter.setBrush(QBrush(self.right_hand_color))
                    else:
                        cache_painter.setBrush(QBrush(self.unknown_color))
                    
                    cache_painter.drawRect(QRectF(nx, ny, nw, nh))
            
            cache_painter.end()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        painter.drawPixmap(0, 0, self.cached_background)
        
        cx = (self.current_time / self.total_duration) * w
        painter.setPen(QPen(self.cursor_color, 2))
        painter.drawLine(QPointF(cx, 0), QPointF(cx, h))