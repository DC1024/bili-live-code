"""Periodic self-narration so the stream stays active without viewers."""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo('Asia/Shanghai')
INTERVAL = 360
FIRST_DELAY = 120


class MonologueSpeaker(threading.Thread):
    def __init__(self, speech, db_path, started_at, log=print):
        super().__init__(daemon=True, name='monologue')
        self.speech = speech
        self.db_path = db_path
        self.started_at = started_at
        self.log = log
        self._stop = threading.Event()
        self._index = 0

    def stop(self):
        self._stop.set()

    def _stats(self):
        active = done = 0
        try:
            con = sqlite3.connect('file:%s?mode=ro' % self.db_path, uri=True, timeout=5)
            try:
                active = int(con.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('running','claimed','queued')"
                ).fetchone()[0])
                done = int(con.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='completed'"
                ).fetchone()[0])
            finally:
                con.close()
        except Exception:
            pass
        return active, done

    def _text(self):
        now = datetime.datetime.now(LOCAL_TZ)
        uptime = int(time.monotonic() - self.started_at)
        hours, minutes = uptime // 3600, (uptime // 60) % 60
        active, done = self._stats()
        options = [
            '现在是 %d 点 %02d 分，已经播了 %d 小时 %d 分钟。工作区里现在有 %d 个任务在跑，今天完成了 %d 个。我在这儿盯着。'
            % (now.hour, now.minute, hours, minutes, active, done),
            '嗯——汇报一下：电脑和手机都正常，我还在整理手头的东西。今天完成了 %d 个。'
            % done,
            '如果你刚进来，找个座。左边是电脑，中间是手机，右边是活动回执。慢慢看。',
            '现在是 %d 点 %02d 分。没新任务的时候，我检查设备、翻记录。行吧，等下一件事。'
            % (now.hour, now.minute),
            '刚才又做完一些。今天累计 %d 个任务。别硬撑，我继续盯着。'
            % done,
        ]
        text = options[self._index % len(options)]
        self._index += 1
        return text

    def run(self):
        if self._stop.wait(FIRST_DELAY):
            return
        while not self._stop.is_set():
            if self.speech.can_accept():
                text = self._text()
                if not self.speech.say_text(text, caption=text):
                    self.speech.say_line('idle')
                self.log('monologue: ' + text[:60])
            self._stop.wait(INTERVAL)
