#!/usr/bin/env python3
"""Headless Bilibili live streamer.

- Reads the live code from a bili-live-code WebUI instance.
- Captures a desktop screenshot from a read-only HTTP agent endpoint.
- Captures a phone screenshot from an Android agent endpoint.
- Composes a 1920x1080 frame and pushes it to Bilibili RTMP with ffmpeg.
- Optional: Live2D overlay, TTS voice, danmaku replies, scene rotation,
  periodic self-narration and a visible desktop work queue.

All endpoints, tokens and paths are configured with environment variables;
see README.md for the full list. No desktop/OBS is required.
"""
from __future__ import annotations

import base64
import datetime as _datetime
import http.cookiejar
import io
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageStat

from chat_watch import ChatWatcher
from event_speak import EventSpeaker
from live_work import LiveWorker
from monologue import MonologueSpeaker
from speech import SpeechEngine

DATA = Path(os.environ.get('STREAM_DATA_DIR', './data'))
WEBUI_ENV = Path(os.environ.get('BILI_WEBUI_ENV', str(DATA / 'webui.env')))
TOKEN_PC = Path(os.environ.get('HER_PC_TOKEN_FILE', str(DATA / 'her-agent.token')))
TOKEN_PHONE = Path(os.environ.get('ANDROID_TOKEN_FILE', str(DATA / 'android.token')))
LOG_FILE = DATA / 'streamer.log'
FFMPEG_LOG = DATA / 'ffmpeg.log'
PREVIEW = DATA / 'preview.jpg'
LIVE2D_DIR = Path(os.environ.get('STREAM_LIVE2D_DIR', str(DATA / 'live2d')))
LIVE2D_FPS = float(os.environ.get('STREAM_LIVE2D_FPS', '10'))
TTS_ENABLED = os.environ.get('STREAM_TTS', '1') == '1'
LIVE_WORK_ENABLED = os.environ.get('STREAM_LIVE_WORK', '1') == '1'
VOICE_DIR = Path(os.environ.get('STREAM_VOICE_DIR', str(DATA / 'voice')))
BREEZE_ENV = Path(os.environ.get('BREEZE_ENV', str(DATA / 'breeze.env')))

WEBUI = os.environ.get('BILI_WEBUI_URL', 'http://127.0.0.1:8090').rstrip('/')
PC_URL = os.environ.get('HER_PC_URL', 'http://127.0.0.1:19990/action')
PHONE_URL = os.environ.get('HER_ANDROID_URL', 'http://127.0.0.1:19992/shot')
SPACE_DB = Path(os.environ['SPACE_DB']) if os.environ.get('SPACE_DB') else None
LOCAL_TZ = ZoneInfo(os.environ.get('STREAM_TIMEZONE', 'Asia/Shanghai'))
STREAM_TITLE = os.environ.get('STREAM_TITLE', 'B站直播 · 三栏画面')
STREAM_SUBTITLE = os.environ.get('STREAM_SUBTITLE', '')
LIVE2D_CREDIT = os.environ.get('LIVE2D_CREDIT', '')
FONT_PATH = ''
for _candidate in (os.environ.get('STREAM_FONT', ''),
                   '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                   '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
    if _candidate and Path(_candidate).exists():
        FONT_PATH = _candidate
        break
FPS = float(os.environ.get('STREAM_FPS', '10'))
STALE_SECONDS = int(os.environ.get('STREAM_STALE_SECONDS', '240'))
WIDTH, HEIGHT = 1920, 1080
PC_INTERVAL = 0.5
PHONE_INTERVAL = 0.35

DATA.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DATA, 0o700)
except OSError:
    pass


def log(message: str) -> None:
    line = time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + message
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass


def _token(path: Path) -> str:
    try:
        return path.read_text(encoding='ascii').strip()
    except OSError:
        return ''


_FONTS = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _FONTS:
        try:
            _FONTS[size] = ImageFont.truetype(FONT_PATH, size)
        except OSError:
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    image = image.convert('RGB')
    source_w, source_h = image.size
    scale = min(width / source_w, height / source_h)
    return image.resize((max(1, int(source_w * scale)), max(1, int(source_h * scale))),
                        Image.Resampling.LANCZOS)


_FIT_CACHE = {}


def _fit_cached(name: str, image: Image.Image, width: int, height: int) -> Image.Image:
    cached = _FIT_CACHE.get(name)
    if cached and cached[0] is image and cached[1].size == (width, height):
        return cached[1]
    fitted = _fit(image, width, height)
    _FIT_CACHE[name] = (image, fitted)
    return fitted


def _placeholder(width: int, height: int, text: str) -> Image.Image:
    image = Image.new('RGB', (width, height), '#1b1f27')
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width - 1, height - 1], outline='#2a2f3a')
    draw.text((width // 2, height // 2), text, fill='#9aa3b2', font=_font(20), anchor='mm')
    return image


def fetch_pc() -> Image.Image:
    token = _token(TOKEN_PC)
    if not token:
        raise RuntimeError('missing her-agent token')
    request = urllib.request.Request(
        PC_URL, data=json.dumps({'action': 'screen'}).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    if value.get('ok') is not True or not value.get('image'):
        raise RuntimeError(str(value.get('error') or 'screen failed'))
    image = Image.open(io.BytesIO(base64.b64decode(value['image']))).convert('RGB')
    if sum(ImageStat.Stat(image).mean) / 3 < 5:
        raise RuntimeError('电脑画面是黑屏，请检查桌面代理')
    return image


def fetch_phone() -> Image.Image:
    token = _token(TOKEN_PHONE)
    if not token:
        raise RuntimeError('missing android token')
    request = urllib.request.Request(
        PHONE_URL, data=b'{}', method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
    image = Image.open(io.BytesIO(data)).convert('RGB')
    if sum(ImageStat.Stat(image).mean) / 3 < 5:
        raise RuntimeError('手机屏幕是黑屏，可能已熄屏')
    return image


def _agent_post(url, payload, token, timeout=15):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def recover_pc() -> bool:
    token = _token(TOKEN_PC)
    if not token:
        return False
    ok = False
    try:
        _agent_post(PC_URL.rsplit('/action', 1)[0] + '/exec', {
            'argv': ['/usr/bin/xset', '-display', os.environ.get('HER_PC_DISPLAY', ':99'),
                     's', 'off', '-dpms'],
            'timeout_sec': 10}, token)
        ok = True
    except Exception:
        pass
    try:
        _agent_post(PC_URL, {'action': 'click', 'x': 400, 'y': 300}, token)
        ok = True
    except Exception:
        pass
    return ok


def recover_phone() -> bool:
    token = _token(TOKEN_PHONE)
    if not token:
        return False
    try:
        _agent_post(PHONE_URL.rsplit('/shot', 1)[0] + '/key', {'key': 'wake'}, token)
        return True
    except Exception:
        return False


def notify_alert(message: str) -> None:
    log('ALERT ' + message)
    url = os.environ.get('STREAM_ALERT_URL', '').strip()
    if not url:
        return
    body = json.dumps({'msg_type': 'text',
                       'content': {'text': '直播提醒：' + message}}).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        log('alert webhook sent')
    except Exception as error:
        log('alert webhook failed: ' + str(error)[:100])


class Capture:
    def __init__(self, name: str, fetch, interval: float, recover=None):
        self.name, self.fetch, self.interval = name, fetch, interval
        self.recover = recover
        self.image = None
        self.at = 0.0
        self.error = ''
        self.failures = 0
        self.last_recover = 0.0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='capture-' + name)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                image = self.fetch()
                self.image = image
                self.at = time.monotonic()
                self.error = ''
                self.failures = 0
            except Exception as error:
                self.error = str(error)[:120]
                self.failures += 1
                now = time.monotonic()
                if self.failures >= 2 and self.recover and now - self.last_recover >= 90:
                    self.last_recover = now
                    try:
                        if self.recover():
                            log(self.name + ' recover attempted')
                    except Exception as recover_error:
                        log(self.name + ' recover failed: ' + str(recover_error)[:80])
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.05, self.interval - elapsed))

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)


KIND_LABELS = {
    'task.dispatch': '派发', 'task.accepted': '已接收', 'task.result': '结果',
    'control.hold': '停止', 'control.pause': '暂停', 'control.resume': '继续',
    'context.rotated': '上下文压缩', 'device.stage': '设备',
    'memory.saved': '记忆', 'reasoning.selected': '思考级别',
}


def _local_time(value) -> str:
    try:
        stamp = _datetime.datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_datetime.timezone.utc)
        return stamp.astimezone(LOCAL_TZ).strftime('%H:%M:%S')
    except (TypeError, ValueError):
        return '--:--:--'


def _persona(value) -> str:
    return {'biao': '表', 'li': '里'}.get(str(value), str(value or ''))


def _event_line(row) -> str:
    stamp = _local_time(row[0])
    kind = row[1]
    persona = _persona(row[2])
    task = str(row[3] or '')[:8]
    try:
        payload = json.loads(row[4] or '{}')
    except ValueError:
        payload = {}
    label = KIND_LABELS.get(kind, kind)
    detail = ''
    if kind == 'task.result':
        detail = str(payload.get('status') or '')
        failure = (payload.get('failure') or {}).get('code')
        if failure:
            detail += ' / ' + str(failure)
    elif kind == 'control.hold':
        detail = str(payload.get('source') or '')
    elif kind == 'context.rotated':
        detail = str(payload.get('phase') or '')
    elif kind == 'device.stage':
        detail = '%s/%s %s' % (payload.get('device') or '', payload.get('action') or '',
                                payload.get('phase') or '')
    elif kind == 'reasoning.selected':
        detail = str(payload.get('level') or '')
    elif kind in ('control.pause', 'control.resume'):
        detail = str(payload.get('message') or '')[:40]
    text = f'{stamp} {persona} {label} {task}'.rstrip()
    if detail:
        text += ' · ' + detail
    return text


class SpaceFeed:
    def __init__(self):
        self.lines = ['空间终端启动中…']
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='space-feed')

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def fetch(self):
        if not SPACE_DB:
            return ['未配置 SPACE_DB，右栏仅作占位。']
        try:
            con = sqlite3.connect('file:%s?mode=ro' % SPACE_DB, uri=True, timeout=5)
            try:
                active = con.execute(
                    "SELECT persona,status,request_key FROM tasks "
                    "WHERE status IN ('running','claimed','queued') ORDER BY time DESC LIMIT 2").fetchall()
                rows = con.execute(
                    "SELECT time,kind,persona,task_id,payload FROM events "
                    "WHERE kind IN ('task.dispatch','task.accepted','task.result','control.hold',"
                    "'control.pause','control.resume','context.rotated','device.stage',"
                    "'memory.saved','reasoning.selected') ORDER BY seq DESC LIMIT 40").fetchall()
            finally:
                con.close()
        except Exception as error:
            return ['终端读取失败：' + str(error)[:80]]
        header = 'IDLE 等待新任务'
        if active:
            parts = []
            for persona, status, key in active:
                parts.append('%s %s %s' % (_persona(persona), status, str(key or '')[-14:]))
            header = 'ACTIVE ' + ' | '.join(parts)
        lines = [header]
        for row in reversed(rows):
            lines.append(_event_line(row))
        return lines[-32:]

    def _run(self):
        while not self.stop_event.is_set():
            self.lines = self.fetch()
            self.stop_event.wait(2)


def _clip_text(draw, text, font, width) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while len(text) > 1 and draw.textlength(text + '…', font=font) > width:
        text = text[:-1]
    return text + '…'


_live2d_frames = None


def live2d_frames():
    global _live2d_frames
    if _live2d_frames is not None:
        return _live2d_frames
    frames = []
    try:
        paths = sorted(LIVE2D_DIR.glob('frame_*.png'))
        for path in paths:
            image = Image.open(path).convert('RGBA')
            image.load()
            frames.append(image)
        if frames:
            log('live2d loaded: %d frames from %s' % (len(frames), LIVE2D_DIR))
        else:
            log('live2d frames missing: %s' % LIVE2D_DIR)
    except Exception as error:
        log('live2d load failed: ' + str(error)[:160])
    _live2d_frames = frames
    return _live2d_frames


def _hms(seconds) -> str:
    seconds = int(max(0, seconds))
    return '%02d:%02d:%02d' % (seconds // 3600, (seconds // 60) % 60, seconds % 60)


def compose(pc_image, phone_image, pc_error, phone_error, terminal_lines,
            live2d=None, voice=None, meta=None) -> Image.Image:
    meta = meta or {}
    scene = str(meta.get('scene') or 'full')
    if scene not in ('full', 'pc', 'duo'):
        scene = 'full'
    canvas = Image.new('RGB', (WIDTH, HEIGHT), '#0f1115')
    draw = ImageDraw.Draw(canvas)
    draw.text((30, 18), STREAM_TITLE, fill='#e8eaf0', font=_font(34))
    draw.ellipse([WIDTH - 190, 30, WIDTH - 176, 44], fill='#ff5c5c')
    draw.text((WIDTH - 164, 18), time.strftime('%H:%M:%S'), fill='#9aa3b2', font=_font(26))
    scene_label = {'full': '全景', 'pc': '电脑特写', 'duo': '电脑+手机'}[scene]
    draw.text((WIDTH - 540, 22), '场景：' + scene_label, fill='#6b7280', font=_font(18))

    if scene == 'pc':
        pc_box = (30, 100, 1860, 940)
        phone_box = None
        term_box = None
    elif scene == 'duo':
        pc_box = (30, 100, 1450, 940)
        phone_box = (1480, 100, 1860, 940)
        term_box = None
    else:
        pc_box = (30, 100, 1130, 940)
        phone_box = (1150, 100, 1450, 940)
        term_box = (1480, 100, 1860, 940)

    draw.text((30, 72), '电脑 · 特写' if scene == 'pc' else '电脑', fill='#9aa3b2', font=_font(22))
    if phone_box:
        draw.text((phone_box[0] + 20, 72), '手机', fill='#9aa3b2', font=_font(22))
    if term_box:
        draw.text((term_box[0], 72), '终端 · 活动回执', fill='#9aa3b2', font=_font(22))

    draw.rectangle(pc_box, outline='#2a2f3a')
    if phone_box:
        draw.rectangle(phone_box, outline='#2a2f3a')
    if term_box:
        draw.rectangle(term_box, fill='#0b0f14', outline='#2a2f3a')

    pc_rect = None
    if pc_image is not None:
        fitted = _fit_cached('pc', pc_image, pc_box[2] - pc_box[0], pc_box[3] - pc_box[1])
        x = pc_box[0] + (pc_box[2] - pc_box[0] - fitted.width) // 2
        y = pc_box[1] + (pc_box[3] - pc_box[1] - fitted.height) // 2
        canvas.paste(fitted, (x, y))
        pc_rect = (x, y, x + fitted.width, y + fitted.height)
    else:
        canvas.paste(_placeholder(pc_box[2] - pc_box[0], pc_box[3] - pc_box[1],
                                  '等待电脑画面…' + ('（' + pc_error + '）' if pc_error else '')),
                     (pc_box[0], pc_box[1]))

    if phone_box:
        if phone_image is not None:
            fitted = _fit_cached('phone', phone_image, phone_box[2] - phone_box[0],
                                 phone_box[3] - phone_box[1])
            x = phone_box[0] + (phone_box[2] - phone_box[0] - fitted.width) // 2
            y = phone_box[1] + (phone_box[3] - phone_box[1] - fitted.height) // 2
            canvas.paste(fitted, (x, y))
        else:
            canvas.paste(_placeholder(phone_box[2] - phone_box[0], phone_box[3] - phone_box[1],
                                      '等待手机画面…' + ('（' + phone_error + '）' if phone_error else '')),
                         (phone_box[0], phone_box[1]))

    if term_box:
        term_font = _font(15)
        draw.text((term_box[0] + 12, term_box[1] + 12), 'DSH SPACE TERMINAL',
                  fill='#3ddc97', font=_font(18))
        y = term_box[1] + 42
        for index, line in enumerate((terminal_lines or ['等待活动回执…'])[:36]):
            color = '#8ecdf5' if index == 0 and line.startswith('ACTIVE') else '#c9d1d9'
            draw.text((term_box[0] + 12, y), _clip_text(draw, line, term_font, 356),
                      fill=color, font=term_font)
            y += 22

    if live2d is not None:
        if pc_rect:
            ox = pc_rect[2] - live2d.width - 4
            oy = pc_rect[3] - live2d.height - 4
        else:
            ox = pc_box[2] - live2d.width - 4
            oy = pc_box[3] - live2d.height - 4
        canvas.paste(live2d, (max(pc_box[0], ox), max(pc_box[1], oy)), live2d)

    if voice and voice.get('speaking'):
        level = float(voice.get('level') or 0.0)
        base_x = (pc_rect[0] + 16) if pc_rect else (pc_box[0] + 16)
        base_y = (pc_rect[3] - 16) if pc_rect else (pc_box[3] - 16)
        phase = time.time() * 2.0 * math.pi
        for index in range(6):
            height = 4 + int(26 * min(1.0, level)
                             * (0.35 + 0.65 * abs(math.sin(phase * 1.7 + index * 0.8))))
            draw.rectangle([base_x + index * 10, base_y - height,
                            base_x + index * 10 + 6, base_y], fill='#3ddc97')
        caption = str(voice.get('text') or '')[:42]
        if caption:
            box_y = base_y - 58
            width = 10 + int(draw.textlength(caption, font=_font(16)))
            draw.rectangle([base_x, box_y, base_x + width, box_y + 24],
                           fill='#0f1115', outline='#2a2f3a')
            draw.text((base_x + 5, box_y + 4), caption, fill='#c9d1d9', font=_font(16))

    ticker = '弹幕 · ' + (str(meta.get('danmaku') or '') or '等待弹幕…')
    draw.text((30, 948), _clip_text(draw, ticker, _font(16), WIDTH - 430),
              fill='#8ecdf5', font=_font(16))
    draw.text((WIDTH - 360, 948),
              '在线 %d · 已播 %s' % (int(meta.get('online') or 0),
                                    _hms(int(meta.get('uptime') or 0))),
              fill='#9aa3b2', font=_font(16))

    draw.text((30, 1010), 'HEADLESS STREAM · %g fps' % FPS, fill='#9aa3b2', font=_font(18))
    if STREAM_SUBTITLE:
        draw.text((WIDTH - 360, 1010), STREAM_SUBTITLE, fill='#9aa3b2', font=_font(18))
    if LIVE2D_CREDIT:
        draw.text((30, 1040), LIVE2D_CREDIT, fill='#6b7280', font=_font(15))
    return canvas


def combine_url(addr: str, code: str) -> str:
    addr = str(addr or '').strip()
    code = str(code or '').strip()
    if not addr or not code:
        raise RuntimeError('empty stream address or code')
    if code.startswith('?') or code.startswith('&'):
        return addr + code
    if 'streamname=' in code or 'key=' in code:
        return addr + ('&' if '?' in addr else '?') + code
    return addr.rstrip('/') + '/' + code.lstrip('/')


class WebUIClient:
    def __init__(self, password: str):
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def login(self):
        data = urllib.parse.urlencode({'password': self.password}).encode()
        request = urllib.request.Request(WEBUI + '/login', data=data, method='POST')
        with self.opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError('WebUI login HTTP %s' % response.status)

    def status(self):
        try:
            with self.opener.open(WEBUI + '/api/status', timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            self.login()
            with self.opener.open(WEBUI + '/api/status', timeout=15) as response:
                return json.load(response)


def read_webui_password() -> str:
    text = WEBUI_ENV.read_text(encoding='utf-8')
    match = re.search(r'^BILI_WEBUI_PASSWORD=(.+)$', text, re.MULTILINE)
    if not match:
        raise RuntimeError('missing BILI_WEBUI_PASSWORD in ' + str(WEBUI_ENV))
    return match.group(1).strip()


class StreamSession:
    def __init__(self, url: str, room_id: str = '', webui_password: str = ''):
        self.url = url
        self.room_id = str(room_id or '')
        self.webui_password = webui_password
        self.proc = None
        self.pc = Capture('pc', fetch_pc, PC_INTERVAL, recover=recover_pc)
        self.phone = Capture('phone', fetch_phone, PHONE_INTERVAL, recover=recover_phone)
        self.space = SpaceFeed()
        self.stop_event = threading.Event()
        self.thread = None
        self.live2d = live2d_frames()
        self.speech = SpeechEngine(DATA, VOICE_DIR, BREEZE_ENV, log=log) if TTS_ENABLED else None
        self.events = None
        self.chat = None
        self.live_worker = None
        self.monologue = None
        self.scene_rotation = os.environ.get('STREAM_SCENE_ROTATION', '1') == '1'
        self.online = 0
        self.started_at = time.monotonic()

    def _scene(self):
        if not self.scene_rotation:
            return 'full'
        return ('full', 'pc', 'duo')[int((time.monotonic() - self.started_at) // 240) % 3]

    def start(self):
        self.pc.start()
        self.phone.start()
        self.space.start()
        if self.speech:
            self.speech.start()
            self.speech.say_line('boot')
        audio_input = (
            ['-f', 's16le', '-ar', '24000', '-ac', '1', '-i', str(self.speech.fifo)]
            if self.speech else
            ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100']
        )
        command = [
            '/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-f', 'image2pipe', '-framerate', str(FPS), '-i', '-',
            *audio_input,
            '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
            '-b:v', '3500k', '-maxrate', '3500k', '-bufsize', '7000k',
            '-pix_fmt', 'yuv420p', '-g', str(max(5, int(FPS * 2))),
            '-c:a', 'aac', '-b:a', '128k',
            '-f', 'flv', self.url,
        ]
        FFMPEG_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            FFMPEG_LOG.touch(exist_ok=True)
            os.chmod(FFMPEG_LOG, 0o600)
        except OSError:
            pass
        handle = FFMPEG_LOG.open('ab')
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=handle, start_new_session=True)
        if self.speech:
            if SPACE_DB and SPACE_DB.exists():
                self.events = EventSpeaker(self.speech, str(SPACE_DB), log=log)
                self.events.start()
                self.monologue = MonologueSpeaker(
                    self.speech, str(SPACE_DB), self.started_at, log=log)
                self.monologue.start()
            if self.room_id:
                self.chat = ChatWatcher(self.speech, self.room_id, log=log,
                                        webui_url=WEBUI, webui_password=self.webui_password)
                self.chat.start()
            if LIVE_WORK_ENABLED:
                self.live_worker = LiveWorker(
                    PC_URL, str(TOKEN_PC), str(SPACE_DB) if SPACE_DB else '', self.speech, log=log)
                self.live_worker.start()
            log('speech enabled; dynamic=%s room=%s work=%s' % (
                self.speech.available(), self.room_id, LIVE_WORK_ENABLED))
        self.thread = threading.Thread(target=self._run, daemon=True, name='stream-frames')
        self.thread.start()

    def _run(self):
        interval = 1.0 / FPS
        while not self.stop_event.is_set():
            if self.proc is None or self.proc.poll() is not None:
                break
            started = time.monotonic()
            overlay = None
            if self.live2d:
                index = int((started - self.started_at) * LIVE2D_FPS) % len(self.live2d)
                overlay = self.live2d[index]
            voice = self.speech.state() if self.speech else None
            meta = {
                'online': self.online,
                'uptime': int(time.monotonic() - self.started_at),
                'danmaku': self.chat.ticker() if self.chat else '',
                'scene': self._scene(),
            }
            frame = compose(self.pc.image, self.phone.image, self.pc.error, self.phone.error,
                            self.space.lines, overlay, voice, meta)
            buffer = io.BytesIO()
            frame.save(buffer, format='JPEG', quality=86)
            try:
                self.proc.stdin.write(buffer.getvalue())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            self.stop_event.wait(max(0.02, interval - (time.monotonic() - started)))

    def exited(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def stop(self):
        self.stop_event.set()
        for component in (self.chat, self.events, self.monologue, self.live_worker):
            if component:
                component.stop()
        self.pc.stop()
        self.phone.stop()
        self.space.stop()
        if self.speech:
            self.speech.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def preview() -> int:
    try:
        pc = fetch_pc()
    except Exception as error:
        pc = None
        log('preview pc capture failed: ' + str(error)[:120])
    try:
        phone = fetch_phone()
    except Exception as error:
        phone = None
        log('preview phone capture failed: ' + str(error)[:120])
    feed = SpaceFeed()
    terminal_lines = feed.fetch()
    frames = live2d_frames()
    overlay = frames[0] if frames else None
    frame = compose(pc, phone, '' if pc else 'capture failed', '' if phone else 'capture failed',
                    terminal_lines, overlay)
    frame.save(PREVIEW, format='JPEG', quality=90)
    print(str(PREVIEW))
    return 0


def main() -> int:
    if '--preview' in sys.argv:
        return preview()
    try:
        password = read_webui_password()
    except Exception as error:
        log('cannot start: ' + str(error)[:160])
        return 1
    client = WebUIClient(password)
    try:
        client.login()
    except Exception as error:
        log('WebUI login failed: ' + str(error)[:160])
    log('streamer started; waiting for live code')
    current = None
    stopped = False
    last_state = 'init'
    last_error_at = 0.0
    cut_key = None
    quick = {'key': None, 'count': 0, 'at': 0.0}
    offline_alert_key = None

    def _shutdown(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stopped:
        status = None
        try:
            status = client.status()
        except Exception as error:
            now = time.monotonic()
            if now - last_error_at >= 60:
                last_error_at = now
                log('WebUI status failed: ' + str(error)[:120])
            time.sleep(5)
            continue

        stream = status.get('stream')
        if current is not None:
            try:
                current.online = int(status.get('online') or 0)
            except (TypeError, ValueError):
                current.online = 0
        try:
            if stream and stream.get('rtmp_addr') and stream.get('rtmp_code'):
                key = str(stream.get('started_at') or '')
                if cut_key == key:
                    if last_state != 'cut':
                        log('stream cut suspected; waiting for a new live code')
                        last_state = 'cut'
                    time.sleep(5)
                    continue
                try:
                    age = time.time() - float(stream.get('started_at') or 0)
                except (TypeError, ValueError):
                    age = 10 ** 9
                if (age > STALE_SECONDS and status.get('live_status') != 1
                        and (current is None or current.exited())):
                    if current is not None:
                        current.stop()
                        current = None
                    if last_state != 'stale':
                        log('live code expired (%ds old); start live again in WebUI' % int(age))
                        last_state = 'stale'
                    time.sleep(5)
                    continue
                if current is not None and current.exited():
                    lifetime = time.monotonic() - current.started_at
                    if lifetime < 20:
                        now = time.monotonic()
                        if quick['key'] != key or now - quick['at'] > 120:
                            quick.update(key=key, count=0, at=now)
                        quick['count'] += 1
                        quick['at'] = now
                        if quick['count'] >= 3:
                            cut_key = key
                            current.stop()
                            current = None
                            notify_alert('连续 3 次推流被断开，疑似被 B 站切断；请在 WebUI 重新开播')
                            time.sleep(5)
                            continue
                url = combine_url(stream['rtmp_addr'], stream['rtmp_code'])
                if current is None or current.url != url or current.exited():
                    if current is not None:
                        current.stop()
                    current = StreamSession(url, status.get('room_id') or '', password)
                    current.start()
                    if last_state != 'live':
                        log('live code received; ffmpeg started')
                        last_state = 'live'
            else:
                if current is not None:
                    current.stop()
                    current = None
                    log('live code cleared; ffmpeg stopped')
                if last_state != 'idle':
                    log('no live code; streamer idle')
                    last_state = 'idle'
        except Exception as error:
            log('stream start failed: ' + str(error)[:160])
            if current is not None:
                current.stop()
                current = None
            last_state = 'error'
        if stream and current is not None and status.get('live_status') == 2:
            key = str(stream.get('started_at') or '')
            if offline_alert_key != key:
                offline_alert_key = key
                notify_alert('直播间状态已变为未开播，可能被切断；请检查 WebUI')
        time.sleep(5)

    if current is not None:
        current.stop()
    log('streamer stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
