"""Bilibili live-room WebSocket for gift / follow / enter events."""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib
import http.cookiejar
from pathlib import Path

import brotli
import websocket

HEADER = struct.Struct('>IHHII')
AUTH_OP = 7
HEARTBEAT_OP = 2
MESSAGE_OP = 5
AUTH_REPLY_OP = 8
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36')
INFO_API = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo'
SPI_API = 'https://api.bilibili.com/x/frontend/finger/spi'
WEBUI_URL = os.environ.get('BILI_WEBUI_URL', 'http://127.0.0.1:8090').rstrip('/')
WEBUI_ENV = Path(os.environ.get('BILI_WEBUI_ENV', './data/webui.env'))


class BiliLiveSocket(threading.Thread):
    def __init__(self, room_id, on_event, log=print):
        super().__init__(daemon=True, name='bili-ws')
        self.room_id = str(room_id)
        self.on_event = on_event
        self.log = log
        self._stop = threading.Event()
        self._ws = None
        self._send_lock = threading.Lock()
        self._seq = 0
        self._cookie = ''
        self._last_error = 0.0

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as error:
                now = time.monotonic()
                if now - self._last_error >= 60:
                    self._last_error = now
                    self.log('bili ws: ' + str(error)[:140])
                self._stop.wait(5)

    def _buvid(self):
        if self._cookie:
            return self._cookie
        try:
            request = urllib.request.Request(SPI_API, headers={'User-Agent': UA})
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response).get('data') or {}
            b3 = data.get('b_3') or ''
            b4 = data.get('b_4') or ''
            if b3:
                self._cookie = 'buvid3=%s; buvid4=%s' % (b3, b4)
        except Exception:
            self._cookie = ''
        return self._cookie

    def _webui_password(self):
        value = os.environ.get('BILI_WEBUI_PASSWORD', '').strip()
        if value:
            return value
        try:
            text = WEBUI_ENV.read_text(encoding='utf-8')
            match = re.search(r'^BILI_WEBUI_PASSWORD=(.+)$', text, re.MULTILINE)
            return match.group(1).strip() if match else ''
        except OSError:
            return ''

    def _webui_info(self):
        password = self._webui_password()
        if not password:
            return None
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            login = urllib.parse.urlencode({'password': password}).encode()
            opener.open(urllib.request.Request(
                WEBUI_URL + '/login', data=login, method='POST'), timeout=15)
            query = urllib.parse.urlencode({'room_id': self.room_id})
            with opener.open(WEBUI_URL + '/api/danmu-token?' + query, timeout=20) as response:
                value = json.load(response)
            token = str(value.get('token') or '')
            host = str(value.get('host') or '')
            if token and host:
                return (token, host, int(value.get('port') or 443), int(value.get('uid') or 0),
                        str(value.get('buvid') or ''))
        except Exception as error:
            self.log('bili ws webui token: ' + str(error)[:100])
        return None

    def _danmu_info(self):
        info = self._webui_info()
        if info:
            return info
        headers = {'User-Agent': UA, 'Referer': 'https://live.bilibili.com/' + self.room_id}
        cookie = self._buvid()
        buvid = ''
        if cookie:
            headers['Cookie'] = cookie
            for part in cookie.split(';'):
                if part.strip().startswith('buvid3='):
                    buvid = part.split('=', 1)[1].strip()
        request = urllib.request.Request(
            INFO_API + '?id=%s&type=0' % self.room_id, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
        if value.get('code') != 0:
            raise RuntimeError('danmu info code %s' % value.get('code'))
        data = value.get('data') or {}
        token = data.get('token') or ''
        hosts = data.get('host_list') or []
        if not token or not hosts:
            raise RuntimeError('danmu info missing token/host')
        host = hosts[0]
        return (token, host.get('host') or 'broadcastlv.chat.bilibili.com',
                int(host.get('wss_port') or 443), 0, buvid)

    def _send(self, op, body, protover=1):
        total = 16 + len(body)
        packet = HEADER.pack(total, 16, protover, op, self._seq) + body
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        with self._send_lock:
            if self._ws is not None:
                self._ws.send_binary(packet)

    def _session(self):
        token, host, port, uid, buvid = self._danmu_info()
        url = 'wss://%s:%s/sub' % (host, port)
        cookie = self._buvid()
        headers = ['User-Agent: ' + UA]
        if cookie:
            headers.append('Cookie: ' + cookie)
        ws = websocket.create_connection(
            url, timeout=15, header=headers, origin='https://live.bilibili.com',
            enable_multithread=True)
        self._ws = ws
        auth = json.dumps({
            'uid': int(uid), 'roomid': int(self.room_id), 'protover': 2,
            'platform': 'web', 'type': 2, 'key': token, 'buvid': buvid,
        }, separators=(',', ':')).encode('utf-8')
        self._send(AUTH_OP, auth)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                frame = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(frame, (bytes, bytearray)):
                continue
            if self._auth_reply(bytes(frame)):
                break
        else:
            ws.close()
            raise RuntimeError('auth reply timeout')
        self.log('bili ws connected room=%s host=%s uid=%s' % (self.room_id, host, uid))
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(heartbeat_stop,),
                                     daemon=True, name='bili-ws-heartbeat')
        heartbeat.start()
        try:
            while not self._stop.is_set():
                try:
                    frame = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    break
                if isinstance(frame, (bytes, bytearray)):
                    self._parse(bytes(frame))
        finally:
            heartbeat_stop.set()
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None

    def _auth_reply(self, data):
        if len(data) < 16:
            return False
        total, header_len, protover, op, _ = HEADER.unpack(data[:16])
        if op != AUTH_REPLY_OP:
            return False
        body = data[header_len:total]
        try:
            value = json.loads(body.decode('utf-8'))
        except ValueError:
            return False
        if value.get('code') != 0:
            raise RuntimeError('auth rejected code %s' % value.get('code'))
        return True

    def _heartbeat(self, stop_event):
        while not stop_event.is_set() and not self._stop.is_set():
            if stop_event.wait(30):
                break
            try:
                self._send(HEARTBEAT_OP, b'')
            except Exception:
                break

    def _parse(self, data):
        if len(data) < 16:
            return
        total, header_len, protover, op, _ = HEADER.unpack(data[:16])
        body = data[header_len:total]
        if op == AUTH_REPLY_OP or op == 3:
            return
        if op == MESSAGE_OP:
            if protover == 2:
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    return
                self._parse_nested(body)
            elif protover == 3:
                try:
                    body = brotli.decompress(body)
                except Exception:
                    return
                self._parse_nested(body)
            else:
                self._command(body)

    def _parse_nested(self, data):
        offset = 0
        while offset + 16 <= len(data):
            total, header_len, protover, op, _ = HEADER.unpack(data[offset:offset + 16])
            if total < 16 or offset + total > len(data):
                break
            body = data[offset + header_len:offset + total]
            if op == MESSAGE_OP:
                if protover == 2:
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = b''
                elif protover == 3:
                    try:
                        body = brotli.decompress(body)
                    except Exception:
                        body = b''
                if body:
                    self._command(body)
            offset += total

    def _command(self, raw):
        try:
            value = json.loads(raw.decode('utf-8'))
        except ValueError:
            return
        cmd = str(value.get('cmd') or '').split(':')[0]
        data = value.get('data') or {}
        if cmd == 'INTERACT_WORD':
            name = str(data.get('uname') or (data.get('uinfo') or {}).get('uname') or '').strip()
            kind = {1: 'enter', 2: 'follow', 3: 'share'}.get(int(data.get('msg_type') or 0))
            if name and kind:
                self.on_event(kind, {'name': name})
        elif cmd == 'SEND_GIFT':
            name = str(data.get('uname') or '').strip()
            gift = str(data.get('giftName') or '').strip()
            if name and gift:
                self.on_event('gift', {'name': name, 'gift': gift,
                                       'num': int(data.get('num') or 1)})
        elif cmd == 'GUARD_BUY':
            name = str(data.get('username') or '').strip()
            gift = str(data.get('gift_name') or '').strip()
            if name and gift:
                self.on_event('guard', {'name': name, 'gift': gift,
                                        'num': int(data.get('num') or 1)})
        elif cmd == 'DANMU_MSG':
            info = value.get('info') or data.get('info') or []
            try:
                text = str(info[1] or '').strip()
                user = info[2] or []
                uid = int(user[0] or 0)
                name = str(user[1] or '').strip()
            except (IndexError, TypeError, ValueError):
                return
            if name and text:
                self.on_event('danmaku', {'name': name, 'text': text[:60], 'uid': uid})
        elif cmd == 'SUPER_CHAT_MESSAGE':
            info = data.get('user_info') or {}
            name = str(info.get('uname') or '').strip()
            message = str(data.get('message') or '').strip()
            if name:
                self.on_event('super_chat', {'name': name, 'message': message[:60]})
