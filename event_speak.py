"""Turn space activity events into pre-rendered voice lines."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from voice_lines import LINES

IDLE_SECONDS = 480
MIN_GAP_SECONDS = 20
IDLE_LINES = ('idle', 'thanks_watch', 'idle', 'night')


def _persona(value):
    return {'biao': '表', 'li': '里'}.get(str(value), '')


def _payload(raw):
    try:
        value = json.loads(raw or '{}')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class EventSpeaker(threading.Thread):
    def __init__(self, speech, db_path, log=print):
        super().__init__(daemon=True, name='event-speaker')
        self.speech = speech
        self.db_path = db_path
        self.log = log
        self._stop = threading.Event()
        self._gaps = {}
        self._idle_count = 0

    def stop(self):
        self._stop.set()

    def _max_seq(self):
        try:
            con = sqlite3.connect('file:%s?mode=ro' % self.db_path, uri=True, timeout=5)
            try:
                row = con.execute('SELECT COALESCE(MAX(seq), 0) FROM events').fetchone()
                return int(row[0] or 0)
            finally:
                con.close()
        except Exception:
            return 0

    def _rows(self, last_seq):
        try:
            con = sqlite3.connect('file:%s?mode=ro' % self.db_path, uri=True, timeout=5)
            try:
                return con.execute(
                    "SELECT seq,kind,persona,payload FROM events WHERE seq > ? "
                    "AND kind IN ('task.dispatch','task.result','context.rotated','memory.saved',"
                    "'device.stage') ORDER BY seq LIMIT 40", (last_seq,)).fetchall()
            finally:
                con.close()
        except Exception as error:
            self.log('voice event read failed: ' + str(error)[:100])
            return []

    def _allowed(self, name, gap):
        now = time.monotonic()
        last = self._gaps.get(name, 0.0)
        if now - last < gap:
            return False
        self._gaps[name] = now
        return True

    def _line_for(self, kind, persona, payload):
        if kind == 'task.dispatch':
            if not self._allowed('task_dispatch', 60):
                return None
            return 'task_start_li' if persona == '里' else 'task_start_biao'
        if kind == 'task.result':
            status = str(payload.get('status') or '')
            suffix = '_biao' if persona == '表' else ''
            if status == 'completed':
                return 'task_done' + suffix if self._allowed('task_done', 120) else None
            if status == 'stopped':
                return 'task_stopped' + suffix if self._allowed('task_stopped', 120) else None
            return None
        if kind == 'context.rotated':
            return 'context_rotated' if self._allowed('context_rotated', 180) else None
        if kind == 'memory.saved':
            return 'memory_saved' if self._allowed('memory_saved', 180) else None
        if kind == 'device.stage':
            device = str(payload.get('device') or '')
            action = str(payload.get('action') or '')
            if action in ('screen', 'shot', 'android_view', 'android_ui', 'health', 'phash'):
                return None
            suffix = '_biao' if persona == '表' else ''
            if device == 'desktop':
                return 'device_desktop' + suffix if self._allowed('device_desktop', 180) else None
            if device == 'phone':
                return 'device_phone' + suffix if self._allowed('device_phone', 180) else None
        return None

    def run(self):
        last_seq = self._max_seq()
        last_spoken = time.monotonic()
        while not self._stop.is_set():
            rows = self._rows(last_seq)
            for seq, kind, persona, raw in rows:
                last_seq = max(last_seq, int(seq))
                line = self._line_for(kind, _persona(persona), _payload(raw))
                if not line:
                    continue
                if self.speech.can_accept():
                    self.speech.say_line(line)
                    last_spoken = time.monotonic()
                    self.log('voice line: ' + line)
            if time.monotonic() - last_spoken >= IDLE_SECONDS:
                if self.speech.can_accept():
                    line = IDLE_LINES[self._idle_count % len(IDLE_LINES)]
                    self._idle_count += 1
                    self.speech.say_line(line)
                    last_spoken = time.monotonic()
                    self.log('voice line: ' + line)
            self._stop.wait(2)
