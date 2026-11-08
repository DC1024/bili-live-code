"""Visible desktop work queue for the live stream."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

TERM_BANNER = os.environ.get('STREAM_TERM_BANNER', '远程桌面 · 自动任务')
TERM_USER = os.environ.get('STREAM_TERM_USER', 'her')
TERM_DISPLAY = os.environ.get('STREAM_TERM_DISPLAY', ':99')
TERM_HOME = os.environ.get('STREAM_TERM_HOME', '/home/her')
TERM_LAUNCHER = '''#!/bin/sh
clear
echo "%s"
echo
echo "桌面已唤醒，等待任务..."
echo
exec /bin/bash -i
''' % TERM_BANNER
DECOR_TERM = 'while true; do sleep 5; done'

COMMANDS = [
    'date; echo; uptime',
    'df -h / /home; echo; free -m',
    'ls -la %s | head -20' % TERM_HOME,
    'ps -eo pid,comm,pcpu --sort=-pcpu | head -15',
    'find %s -maxdepth 2 -type f | head -20' % TERM_HOME,
    'python3 -c "import datetime; print(datetime.datetime.now())"',
]
FIRST_DELAY = 90
INTERVAL = 300


class LiveWorker(threading.Thread):
    def __init__(self, pc_url, token_path, db_path, speech, log=print):
        super().__init__(daemon=True, name='live-worker')
        self.pc_url = pc_url
        self.base_url = pc_url.rsplit('/action', 1)[0]
        self.token_path = Path(token_path)
        self.db_path = db_path
        self.speech = speech
        self.log = log
        self._stop = threading.Event()
        self._index = 0

    def stop(self):
        self._stop.set()

    def _token(self):
        try:
            return self.token_path.read_text(encoding='ascii').strip()
        except OSError:
            return ''

    def _post(self, url, payload, timeout=20):
        token = self._token()
        if not token:
            raise RuntimeError('missing her-agent token')
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def _busy(self):
        if not self.db_path:
            return False
        try:
            con = sqlite3.connect('file:%s?mode=ro' % self.db_path, uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('running','claimed','queued')"
                ).fetchone()
                return int(row[0] or 0) > 0
            finally:
                con.close()
        except Exception:
            return False

    def _exec(self, argv, timeout=10):
        return self._post(self.base_url + '/exec', {'argv': argv, 'timeout_sec': timeout})

    def _term_ids(self):
        value = self._exec(['/usr/bin/xdotool', 'search', '--class', 'XTerm'])
        if value.get('ok') and (value.get('stdout') or '').strip():
            return (value.get('stdout') or '').split()
        return []

    def _ensure_term(self):
        if self._term_ids():
            return True
        banner = base64.b64encode(TERM_LAUNCHER.encode('utf-8')).decode('ascii')
        self._exec(['/usr/bin/su', TERM_USER, '-s', '/bin/sh', '-c',
                    'echo ' + banner + ' | base64 -d > /tmp/term-banner.sh'])
        self._exec(['/usr/bin/su', TERM_USER, '-s', '/bin/sh', '-c',
                    'chmod +x /tmp/term-banner.sh'])
        self._exec(['/usr/bin/su', TERM_USER, '-s', '/bin/sh', '-c',
                    'DISPLAY=' + TERM_DISPLAY + ' setsid xterm -geometry 100x28+20+80 '
                    '-fa Monospace -fs 14 -e /bin/sh /tmp/term-banner.sh >/dev/null 2>&1 &'])
        time.sleep(1.5)
        return bool(self._term_ids())

    def _focus(self):
        try:
            value = self._exec(['/usr/bin/xdotool', 'search', '--class', 'XTerm',
                                'windowactivate', '--sync'])
            if value.get('ok') and value.get('exit') == 0:
                return True
        except Exception as error:
            self.log('live work focus failed: ' + str(error)[:100])
        try:
            self._post(self.pc_url, {'action': 'click', 'x': 400, 'y': 300})
            return True
        except Exception:
            return False

    def _run_command(self, command):
        try:
            self._exec(['/usr/bin/pkill', '-f', DECOR_TERM])
        except Exception:
            pass
        if not self._ensure_term() or not self._focus():
            return False
        try:
            self._exec(['/usr/bin/xdotool', 'type', '--clearmodifiers', '--delay', '40', command])
            self._post(self.pc_url, {'action': 'key', 'key': 'ENTER'})
            time.sleep(1.2)
            return True
        except Exception as error:
            self.log('live work failed: ' + str(error)[:100])
            return False

    def run(self):
        if self._stop.wait(FIRST_DELAY):
            return
        while not self._stop.is_set():
            if not self._busy():
                command = COMMANDS[self._index % len(COMMANDS)]
                self._index += 1
                if self.speech:
                    self.speech.say_line('device_desktop')
                if self._run_command(command):
                    self.log('live work: ' + command[:60])
            self._stop.wait(INTERVAL)
