"""Poll public live-room danmaku and answer a few of them with TTS."""
from __future__ import annotations

import collections
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from bili_ws import BiliLiveSocket

HISTORY = 'https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36')
URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)
LONG_DIGITS = re.compile(r'\d{8,}')
GREETINGS = ('你好', '在吗', '嗨', 'hello', 'hi', '早上好', '晚上好', '下午好')
MENTIONS = tuple(x.strip() for x in os.environ.get('STREAM_MENTIONS', '主播').split(',')
                 if x.strip())


def _clean(text):
    value = URL_PATTERN.sub('', str(text or '')).strip()
    return value[:60]


class DanmakuSender:
    """Sends short text replies through the logged-in bili-live-code WebUI."""

    def __init__(self, base_url, password, room_id, log=print):
        self.base = base_url.rstrip('/')
        self.password = password
        self.room_id = str(room_id)
        self.log = log
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.lock = threading.Lock()
        self._login_at = 0.0
        self._last_send = 0.0
        self._failures = 0
        self._blocked_until = 0.0
        self.csrf = ''
        self.uid = 0

    def _login(self):
        data = urllib.parse.urlencode({'password': self.password}).encode()
        request = urllib.request.Request(self.base + '/login', data=data, method='POST')
        with self.opener.open(request, timeout=15) as response:
            if response.status not in (200, 204):
                raise RuntimeError('WebUI login HTTP %s' % response.status)
        with self.opener.open(self.base + '/', timeout=15) as response:
            page = response.read().decode('utf-8', 'replace')
        match = re.search(r'const CSRF = "([^"]+)"', page)
        if not match:
            raise RuntimeError('WebUI CSRF token missing')
        self.csrf = match.group(1)
        try:
            with self.opener.open(self.base + '/api/status', timeout=15) as response:
                self.uid = int(json.load(response).get('uid') or 0)
        except Exception:
            self.uid = 0
        self._login_at = time.monotonic()

    def _post(self, path, payload):
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.base + path, data=data, method='POST',
            headers={'Content-Type': 'application/json', 'X-CSRF': self.csrf})
        with self.opener.open(request, timeout=15) as response:
            return json.load(response)

    def send(self, text):
        text = str(text or '').strip()[:40]
        if not text:
            return False
        now = time.monotonic()
        if now < self._blocked_until or now - self._last_send < 20:
            return False
        with self.lock:
            try:
                if now - self._login_at > 300:
                    self._login()
                try:
                    value = self._post('/api/danmaku', {'room_id': self.room_id, 'text': text})
                except urllib.error.HTTPError as error:
                    if error.code not in (401, 403):
                        raise
                    self._login()
                    value = self._post('/api/danmaku', {'room_id': self.room_id, 'text': text})
                if value.get('ok') is True:
                    self._last_send = now
                    self._failures = 0
                    self.log('danmaku sent: ' + text)
                    return True
                self.log('danmaku rejected: ' + str(value)[:80])
            except Exception as error:  # noqa: BLE001 发送失败不打断直播
                self._failures += 1
                if self._failures >= 3:
                    self._blocked_until = time.monotonic() + 600
                    self._failures = 0
                    self.log('danmaku send paused 10min: ' + str(error)[:100])
                else:
                    self.log('danmaku send failed: ' + str(error)[:100])
        return False


class ChatWatcher(threading.Thread):
    def __init__(self, speech, room_id, log=print, webui_url='', webui_password=''):
        super().__init__(daemon=True, name='chat-watcher')
        self.speech = speech
        self.room_id = str(room_id)
        self.log = log
        self.headers = {'User-Agent': UA, 'Referer': 'https://live.bilibili.com/' + self.room_id}
        self._stop = threading.Event()
        self._last_error = 0.0
        self._last_ws = 0.0
        self.recent = collections.deque(maxlen=8)
        self.ws = BiliLiveSocket(room_id, self._on_ws_event, log=log)
        self.sender = (DanmakuSender(webui_url, webui_password, room_id, log=log)
                       if webui_url and webui_password else None)
        self._last_reply = 0.0
        self._per_user = {}

    def ticker(self):
        return ' · '.join(list(self.recent)[-3:])[:120]

    def stop(self):
        self._stop.set()
        self.ws.stop()

    def _on_ws_event(self, kind, payload):
        name = str(payload.get('name') or '').strip()[:8]
        if not name:
            return
        if kind == 'danmaku':
            self._handle_chat(name, payload.get('text'), payload.get('uid'))
            return
        now = time.monotonic()
        text = ''
        if kind == 'enter':
            self.recent.append('进场：' + name)
            if now - self._last_ws < 60:
                return
            text = '欢迎 %s 来到直播间。' % name
        elif kind == 'follow':
            self.recent.append('关注：' + name)
            if now - self._last_ws < 30:
                return
            text = '谢谢 %s 的关注！' % name
        elif kind == 'gift':
            gift = str(payload.get('gift') or '')[:12]
            count = int(payload.get('num') or 1)
            self.recent.append('礼物：%s 送出 %s×%d' % (name, gift, count))
            if now - self._last_ws < 15:
                return
            text = '谢谢 %s 送的 %d 个 %s！' % (name, count, gift)
        elif kind == 'guard':
            gift = str(payload.get('gift') or '')[:12]
            self.recent.append('上舰：%s 开通 %s' % (name, gift))
            if now - self._last_ws < 10:
                return
            text = '谢谢 %s 开通了 %s！' % (name, gift)
        elif kind == 'super_chat':
            message = str(payload.get('message') or '')[:40]
            self.recent.append('醒目留言：%s' % name)
            if now - self._last_ws < 10:
                return
            text = '感谢 %s 的醒目留言：%s' % (name, message)
        if not text:
            return
        if not self.speech.can_accept():
            return
        if self.speech.say_text(text, caption=text):
            self._last_ws = now
            self.log('ws voice: ' + text)
            if self.sender and kind in ('gift', 'guard', 'super_chat'):
                self.sender.send(text)

    def _fetch(self):
        query = urllib.parse.urlencode({'roomid': self.room_id, 'room_type': 0})
        request = urllib.request.Request(HISTORY + '?' + query, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = json.load(response)
        except (urllib.error.URLError, ValueError, OSError) as error:
            now = time.monotonic()
            if now - self._last_error >= 120:
                self._last_error = now
                self.log('chat poll failed: ' + str(error)[:100])
            return []
        if value.get('code') != 0:
            return []
        return (value.get('data') or {}).get('room') or []

    def _key(self, message):
        explicit = message.get('id_str') or message.get('id')
        if explicit:
            return str(explicit)
        return '%s|%s|%s' % (message.get('timeline'), message.get('uid'), message.get('text'))

    def _reply(self, name, text):
        lowered = text.lower()
        if any(word in lowered for word in GREETINGS):
            return '%s，你好呀，我在直播。' % name
        if '?' in text or '？' in text:
            return '%s，这个问题我记下来啦，等会儿慢慢想。' % name
        return '我在呢，%s。' % name

    def _handle_chat(self, name, text, uid=0):
        text = _clean(text)
        if not text or LONG_DIGITS.search(text):
            return
        try:
            uid = int(uid or 0)
        except (TypeError, ValueError):
            uid = 0
        if self.sender and self.sender.uid and uid == self.sender.uid:
            return
        name = str(name or '').strip()[:6] or '这位朋友'
        self.recent.append('%s：%s' % (name, text[:30]))
        lowered = text.lower()
        if not (any(word in text for word in MENTIONS)
                or any(word in lowered for word in GREETINGS)
                or '?' in text or '？' in text):
            return
        now = time.monotonic()
        if now - self._last_reply < 15:
            return
        if now - self._per_user.get(name, 0.0) < 30:
            return
        if not self.speech.can_accept():
            return
        reply = self._reply(name, text)
        spoken = '%s说：%s。%s' % (name, text, reply)
        if self.speech.say_text(spoken, caption=spoken):
            self._last_reply = now
            self._per_user[name] = now
            self.log('chat reply: ' + spoken)
            if self.sender:
                self.sender.send('@%s %s' % (name, reply))

    def run(self):
        if self.sender:
            try:
                self.sender._login()
            except Exception as error:
                self.log('danmaku sender login failed: ' + str(error)[:100])
        self.ws.start()
        seen = set()
        first = True
        while not self._stop.is_set():
            messages = self._fetch()
            if first:
                for message in messages:
                    seen.add(self._key(message))
                first = False
            else:
                for message in reversed(messages):
                    key = self._key(message)
                    if key in seen:
                        continue
                    seen.add(key)
                    self._handle_chat(message.get('nickname'), message.get('text'),
                                      message.get('uid'))
            if len(seen) > 500:
                seen = set(list(seen)[-300:])
            self._stop.wait(3)
