"""B 站直播推流码 WebUI.

轻量封装：扫码/手动 Cookie 登录，设置标题与分区，调用 B 站开播/下播接口，
显示 RTMP/SRT 推流地址与推流码。视频本身仍由 OBS 或 ffmpeg 推送。

风险提示：
- 此工具使用 B 站网页端接口获取第三方推流码，可能绕过平台的第三方开播限制；
  账号是否被限制由平台决定，使用风险自担。
- 请使用专用账号，不要使用主账号；Cookie 等同账号凭据，只保存在本机 volume。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import qrcode
import requests
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

DATA = Path(os.environ.get('BILI_DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)

# 直播姬接口常量（来自公开的网页端开播脚本）
APPKEY = 'aae92bc66f3edfab'
APPSEC = 'af125a0d5279fd576c1b4418a3e8276d'
LIVE_VERSION = os.environ.get('BILI_LIVE_VERSION', '7.47.0.10269')
LIVE_BUILD = os.environ.get('BILI_LIVE_BUILD', '10269')

COOKIE_FILE = DATA / 'cookies.json'
CONFIG_FILE = DATA / 'config.json'
STREAM_FILE = DATA / 'stream.json'
PARTITIONS_CACHE = DATA / 'partitions.json'
SECRET_FILE = DATA / 'secret_key'
PASSWORD_FILE = DATA / 'webui_password'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36')
COMMON = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'user-agent': UA,
}
PASSPORT_HEADERS = {**COMMON, 'origin': 'https://www.bilibili.com',
                    'referer': 'https://www.bilibili.com/'}
LIVE_HEADERS = {
    **COMMON,
    'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'origin': 'https://link.bilibili.com',
    'referer': 'https://link.bilibili.com/p/center/index',
}

LOCK = threading.RLock()
BILI = requests.Session()
STATE = {
    'config': {'room_id': '', 'title': os.environ.get('BILI_DEFAULT_TITLE', '我的直播间'),
               'area_v2': '624'},
    'stream': None,
    'nav': None,
    'live': None,
}


class BiliError(RuntimeError):
    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write_private(path: Path, value) -> None:
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)


def _load_secret_key() -> str:
    value = ''
    try:
        value = SECRET_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        value = ''
    if not value:
        value = secrets.token_hex(32)
        SECRET_FILE.write_text(value, encoding='utf-8')
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
    return value


def _load_password() -> str:
    value = os.environ.get('BILI_WEBUI_PASSWORD', '').strip()
    if value:
        return value
    try:
        value = PASSWORD_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        value = ''
    if not value:
        value = secrets.token_urlsafe(18)
        PASSWORD_FILE.write_text(value, encoding='utf-8')
        try:
            os.chmod(PASSWORD_FILE, 0o600)
        except OSError:
            pass
        print('Generated WebUI password in ' + str(PASSWORD_FILE), flush=True)
    return value


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=False,
    MAX_CONTENT_LENGTH=64 * 1024,
)
WEBUI_PASSWORD = _load_password()


def _csrf() -> str:
    return BILI.cookies.get('bili_jct', '') or ''


def _logged_in() -> bool:
    return bool(BILI.cookies.get('SESSDATA'))


def _save_cookies() -> None:
    cookies = {cookie.name: cookie.value for cookie in BILI.cookies}
    _write_private(COOKIE_FILE, {'cookies': cookies, 'saved_at': time.time()})


def _save_config() -> None:
    _write_private(CONFIG_FILE, STATE['config'])


def _bili_get(url: str, *, params=None, headers=None, timeout=15):
    with LOCK:
        response = BILI.get(url, params=params, headers={**COMMON, **(headers or {})},
                            timeout=timeout)
    return response


def _bili_post(url: str, data, *, headers=None, timeout=15):
    with LOCK:
        response = BILI.post(url, data=data, headers={**COMMON, **(headers or {})},
                             timeout=timeout)
    return response


def _json(response) -> dict:
    try:
        value = response.json()
    except ValueError:
        raise BiliError('B 站返回非 JSON（HTTP %s）' % response.status_code)
    if not isinstance(value, dict):
        raise BiliError('B 站返回格式异常')
    if value.get('code') not in (0, None):
        message = str(value.get('message') or value.get('msg') or 'B 站拒绝请求')
        raise BiliError(message, value.get('code'))
    return value


def _qr_data_url(text: str) -> str:
    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    return 'data:image/png;base64,' + encoded


def _live_timestamp() -> str:
    try:
        response = _bili_get('https://api.bilibili.com/x/report/click/now',
                             headers={'referer': 'https://live.bilibili.com/'})
        now = (_json(response).get('data') or {}).get('now')
        if isinstance(now, (int, float)):
            return str(int(now))
    except (BiliError, requests.RequestException):
        pass
    return str(int(time.time()))


def _sign(payload: dict) -> str:
    text = urlencode(list(payload.items()))
    return hashlib.md5((text + APPSEC).encode('utf-8')).hexdigest()


def _nav(force: bool = False) -> dict:
    cached = STATE.get('nav')
    now = time.time()
    if not force and cached and now - cached['at'] < 60:
        return cached['value']
    value = {'logged_in': False, 'uid': None, 'uname': ''}
    try:
        response = _bili_get('https://api.bilibili.com/x/web-interface/nav',
                             headers={'referer': 'https://www.bilibili.com/'})
        data = _json(response).get('data') or {}
        value = {
            'logged_in': bool(data.get('isLogin') and _logged_in()),
            'uid': data.get('mid'),
            'uname': data.get('uname') or '',
        }
    except (BiliError, requests.RequestException):
        pass
    STATE['nav'] = {'at': now, 'value': value}
    return value


def _auto_room() -> str:
    nav = _nav(force=True)
    if not nav.get('logged_in') or not nav.get('uid'):
        raise BiliError('请先登录 B 站账号')
    response = _bili_get('https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld',
                         params={'mid': nav['uid']}, headers=LIVE_HEADERS)
    data = _json(response).get('data') or {}
    room = data.get('roomid') or data.get('room_id') or data.get('roomId')
    if not room:
        raise BiliError('没有找到直播间，请先在 B 站开通直播间')
    return str(room)


def _live_info(force: bool = False) -> dict:
    room = str(STATE['config'].get('room_id') or '').strip()
    if not room:
        return {'live_status': None, 'title': '', 'area_name': '', 'online': 0}
    cached = STATE.get('live')
    now = time.time()
    if not force and cached and now - cached['at'] < 10:
        return cached['value']
    value = {'live_status': None, 'title': '', 'area_name': '', 'online': 0}
    try:
        response = _bili_get('https://api.live.bilibili.com/room/v1/Room/get_info',
                             params={'room_id': room}, headers=LIVE_HEADERS)
        data = _json(response).get('data') or {}
        value = {
            'live_status': data.get('live_status'),
            'title': data.get('title') or '',
            'area_name': data.get('area_name') or '',
            'online': data.get('online') or 0,
        }
    except (BiliError, requests.RequestException):
        pass
    STATE['live'] = {'at': now, 'value': value}
    return value


def _partitions():
    cached = _read_json(PARTITIONS_CACHE, None)
    if cached:
        return cached
    local = Path(__file__).with_name('partitions.json')
    if local.is_file():
        value = _read_json(local, [])
        if value:
            _write_private(PARTITIONS_CACHE, value)
            return value
    try:
        response = _bili_get('https://api.live.bilibili.com/room/v1/Area/getList',
                             params={'show_pinyin': 1}, headers=LIVE_HEADERS)
        value = _json(response).get('data') or []
        if isinstance(value, list) and value:
            _write_private(PARTITIONS_CACHE, value)
            return value
    except (BiliError, requests.RequestException):
        pass
    return []


def _bootstrap_state() -> None:
    saved_config = _read_json(CONFIG_FILE, {})
    if isinstance(saved_config, dict):
        for key in ('room_id', 'title', 'area_v2'):
            if saved_config.get(key):
                STATE['config'][key] = str(saved_config[key])
    saved_cookies = _read_json(COOKIE_FILE, {})
    cookies = saved_cookies.get('cookies') if isinstance(saved_cookies, dict) else None
    if isinstance(cookies, dict):
        for name, value in cookies.items():
            if isinstance(name, str) and isinstance(value, str):
                BILI.cookies.set(name, value, domain='.bilibili.com')
    saved_stream = _read_json(STREAM_FILE, None)
    if isinstance(saved_stream, dict):
        STATE['stream'] = saved_stream


_bootstrap_state()


@app.before_request
def _guard():
    if not request.path.startswith('/api/'):
        return None
    if request.path == '/api/health':
        return None
    if not session.get('auth'):
        return jsonify({'error': '请先登录 WebUI'}), 401
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.headers.get('X-CSRF') != session.get('csrf'):
            return jsonify({'error': 'CSRF 校验失败，请刷新页面'}), 403
    return None


@app.errorhandler(BiliError)
def _bili_error(error):
    return jsonify({'error': str(error), 'code': error.code}), 400


@app.errorhandler(requests.RequestException)
def _request_error(error):
    return jsonify({'error': '连接 B 站失败，请检查网络或代理'}), 502


@app.errorhandler(Exception)
def _unknown_error(error):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception('unhandled')
    return jsonify({'error': '服务内部错误'}), 500


@app.get('/api/health')
def api_health():
    return jsonify({'ok': True, 'service': 'bili-live-code'})


@app.get('/')
def home():
    if not session.get('auth'):
        return render_template('login.html')
    return render_template('index.html', csrf=session.get('csrf', ''))


@app.post('/login')
def login():
    password = request.form.get('password', '')
    if hmac.compare_digest(password, WEBUI_PASSWORD):
        session.clear()
        session['auth'] = True
        session['csrf'] = secrets.token_hex(16)
        return redirect('/')
    return render_template('login.html', error='密码不正确'), 401


@app.post('/logout')
def logout():
    session.clear()
    return redirect('/')


@app.get('/api/status')
def api_status():
    nav = _nav()
    live = _live_info()
    stream = STATE.get('stream') if nav.get('logged_in') else None
    return jsonify({
        'logged_in': nav.get('logged_in'),
        'uid': nav.get('uid'),
        'uname': nav.get('uname'),
        'room_id': STATE['config'].get('room_id') or '',
        'title': STATE['config'].get('title') or '',
        'area_v2': STATE['config'].get('area_v2') or '',
        'live_status': live.get('live_status'),
        'live_title': live.get('title') or '',
        'area_name': live.get('area_name') or '',
        'online': live.get('online') or 0,
        'stream': stream,
    })


def _ensure_buvid() -> None:
    if BILI.cookies.get('buvid3'):
        return
    try:
        response = _bili_get('https://api.bilibili.com/x/frontend/finger/spi',
                             headers={'referer': 'https://www.bilibili.com/'})
        data = _json(response).get('data') or {}
        if data.get('b_3'):
            BILI.cookies.set('buvid3', data['b_3'], domain='.bilibili.com')
        if data.get('b_4'):
            BILI.cookies.set('buvid4', data['b_4'], domain='.bilibili.com')
        BILI.cookies.set('b_nut', str(int(time.time())), domain='.bilibili.com')
    except (BiliError, requests.RequestException):
        pass


WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
           27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
           37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
           22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_WBI = {'key': '', 'at': 0.0}


def _wbi_key() -> str:
    now = time.time()
    if _WBI['key'] and now - _WBI['at'] < 3600:
        return _WBI['key']
    try:
        response = _bili_get('https://api.bilibili.com/x/web-interface/nav',
                             headers={'referer': 'https://www.bilibili.com/'})
        data = _json(response).get('data') or {}
        images = data.get('wbi_img') or {}
        img_key = str(images.get('img_url') or '').rsplit('/', 1)[-1].split('.')[0]
        sub_key = str(images.get('sub_url') or '').rsplit('/', 1)[-1].split('.')[0]
        if img_key and sub_key:
            original = img_key + sub_key
            _WBI['key'] = ''.join(original[i] for i in WBI_TAB if i < len(original))[:32]
            _WBI['at'] = now
    except (BiliError, requests.RequestException):
        pass
    return _WBI['key']


def _wbi_sign(params: dict) -> dict:
    signed = dict(params)
    key = _wbi_key()
    if not key:
        return signed
    signed['wts'] = int(time.time())
    query = urlencode([(name, ''.join(ch for ch in str(value) if ch not in "!'()*"))
                      for name, value in sorted(signed.items())])
    signed['w_rid'] = hashlib.md5((query + key).encode('utf-8')).hexdigest()
    return signed


@app.get('/api/partitions')
def api_partitions():
    return jsonify(_partitions())


@app.get('/api/danmu-token')
def api_danmu_token():
    if not _logged_in():
        raise BiliError('请先登录 B 站账号')
    _ensure_buvid()
    room = str(request.args.get('room_id') or STATE['config'].get('room_id') or '').strip()
    if not room.isdigit():
        raise BiliError('缺少房间号')
    params = _wbi_sign({'id': room, 'type': 0, 'web_location': '444.8'})
    response = _bili_get(
        'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo',
        params=params,
        headers={'referer': 'https://live.bilibili.com/' + room,
                 'origin': 'https://live.bilibili.com'})
    data = _json(response).get('data') or {}
    token = data.get('token')
    hosts = data.get('host_list') or []
    if not token or not hosts:
        raise BiliError('获取弹幕 token 失败')
    host = hosts[0]
    uid = 0
    try:
        uid = int((_nav() or {}).get('uid') or 0)
    except (TypeError, ValueError):
        uid = 0
    return jsonify({
        'token': token,
        'host': host.get('host') or 'broadcastlv.chat.bilibili.com',
        'port': int(host.get('wss_port') or 443),
        'uid': uid,
        'buvid': BILI.cookies.get('buvid3', ''),
        'room_id': room,
    })


@app.post('/api/danmaku')
def api_danmaku():
    if not _logged_in() or not _csrf():
        raise BiliError('请先登录 B 站账号')
    body = request.get_json(silent=True) or {}
    room = str(body.get('room_id') or STATE['config'].get('room_id') or '').strip()
    text = str(body.get('text') or '').strip()
    if not room.isdigit():
        raise BiliError('房间号必须是数字')
    if not text:
        raise BiliError('弹幕内容不能为空')
    if len(text) > 40:
        raise BiliError('弹幕内容不能超过 40 字')
    csrf = _csrf()
    payload = {
        'bubble': 0,
        'color': 16777215,
        'fontsize': 25,
        'mode': 1,
        'msg': text,
        'roomid': room,
        'rnd': int(time.time()),
        'csrf': csrf,
        'csrf_token': csrf,
    }
    value = _json(_bili_post('https://api.live.bilibili.com/msg/send', payload,
                             headers=LIVE_HEADERS))
    data = value.get('data') or {}
    return jsonify({'ok': True, 'mode': data.get('mode'), 'text': text})


@app.get('/api/room/auto')
def api_room_auto():
    room = _auto_room()
    STATE['config']['room_id'] = room
    _save_config()
    return jsonify({'room_id': room})


@app.get('/api/qr')
def api_qr():
    response = _bili_get(
        'https://passport.bilibili.com/x/passport-login/web/qrcode/generate',
        headers=PASSPORT_HEADERS)
    data = _json(response).get('data') or {}
    key = data.get('qrcode_key')
    url = data.get('url')
    if not key or not url:
        raise BiliError('二维码生成失败')
    return jsonify({'key': key, 'image': _qr_data_url(url)})


@app.get('/api/qr/poll')
def api_qr_poll():
    key = request.args.get('key', '').strip()
    if not key:
        raise BiliError('缺少二维码 key')
    response = _bili_get(
        'https://passport.bilibili.com/x/passport-login/web/qrcode/poll',
        params={'qrcode_key': key}, headers=PASSPORT_HEADERS)
    value = response.json()
    data = value.get('data') or {}
    sub_code = data.get('code')
    if value.get('code') == 0 and sub_code == 0:
        nav = _nav(force=True)
        if not nav.get('logged_in'):
            return jsonify({'status': 'error', 'message': '登录回执未生效，请重试'})
        _save_cookies()
        room = ''
        try:
            room = _auto_room()
        except BiliError:
            room = ''
        if room and not STATE['config'].get('room_id'):
            STATE['config']['room_id'] = room
            _save_config()
        return jsonify({'status': 'ok', 'logged_in': True, 'uid': nav.get('uid'),
                        'uname': nav.get('uname'), 'room_id': room})
    messages = {86101: '等待扫码', 86090: '已扫码，等待确认', 86038: '二维码已过期'}
    status = 'pending'
    if sub_code == 86038:
        status = 'expired'
    return jsonify({'status': status, 'message': messages.get(sub_code, '等待确认'),
                    'code': sub_code})


@app.post('/api/cookie')
def api_cookie():
    body = request.get_json(silent=True) or {}
    text = str(body.get('cookie') or '').strip()
    if not text or len(text) > 20000:
        raise BiliError('Cookie 为空或过长')
    BILI.cookies.clear()
    for item in text.split(';'):
        if '=' not in item:
            continue
        name, value = item.strip().split('=', 1)
        if name and value:
            BILI.cookies.set(name, value, domain='.bilibili.com')
    if not _logged_in() or not _csrf():
        raise BiliError('Cookie 缺少 SESSDATA 或 bili_jct')
    nav = _nav(force=True)
    if not nav.get('logged_in'):
        raise BiliError('Cookie 无效或已过期')
    _save_cookies()
    room = ''
    try:
        room = _auto_room()
    except BiliError:
        room = ''
    if room and not STATE['config'].get('room_id'):
        STATE['config']['room_id'] = room
        _save_config()
    return jsonify({'ok': True, 'uid': nav.get('uid'), 'uname': nav.get('uname'),
                    'room_id': room})


@app.post('/api/logout')
def api_logout():
    with LOCK:
        BILI.cookies.clear()
        STATE['stream'] = None
        STATE['nav'] = None
        try:
            COOKIE_FILE.unlink()
        except OSError:
            pass
        try:
            STREAM_FILE.unlink()
        except OSError:
            pass
    return jsonify({'ok': True})


@app.post('/api/start')
def api_start():
    body = request.get_json(silent=True) or {}
    room = str(body.get('room_id') or STATE['config'].get('room_id') or '').strip()
    title = str(body.get('title') or STATE['config'].get('title') or '').strip()
    area = str(body.get('area_v2') or STATE['config'].get('area_v2') or '624').strip()
    if not room.isdigit():
        raise BiliError('房间号必须是数字')
    if not area.isdigit():
        raise BiliError('分区 ID 必须是数字')
    if not _logged_in() or not _csrf():
        raise BiliError('请先登录 B 站账号')
    with LOCK:
        csrf = _csrf()
        title_warning = ''
        if title:
            try:
                _json(_bili_post(
                    'https://api.live.bilibili.com/room/v1/Room/update',
                    {'room_id': room, 'platform': 'pc_link', 'title': title[:40],
                     'csrf_token': csrf, 'csrf': csrf},
                    headers=LIVE_HEADERS))
            except BiliError as error:
                if error.code in (-111, -101):
                    raise
                title_warning = str(error)
        payload = {
            'appkey': APPKEY,
            'area_v2': area,
            'build': LIVE_BUILD,
            'csrf': csrf,
            'platform': 'pc_link',
            'room_id': room,
            'ts': _live_timestamp(),
            'version': LIVE_VERSION,
        }
        payload['sign'] = _sign(payload)
        response = _bili_post('https://api.live.bilibili.com/room/v1/Room/startLive',
                              payload, headers=LIVE_HEADERS)
        try:
            value = response.json()
        except ValueError:
            raise BiliError('B 站返回非 JSON（HTTP %s）' % response.status_code)
        if not isinstance(value, dict):
            raise BiliError('B 站返回格式异常')
        code = value.get('code')
        if code not in (0, None):
            data = value.get('data') or {}
            message = str(value.get('message') or value.get('msg') or 'B 站拒绝开播')
            if code == 60024 and isinstance(data.get('qr'), str):
                return jsonify({
                    'error': message,
                    'code': code,
                    'face_auth': {
                        'type': 'qr',
                        'qr_image': _qr_data_url(data['qr']),
                        'message': '请用哔哩哔哩 App 扫描二维码完成人脸验证，然后点“继续开播”。',
                    },
                }), 400
            if code == 60043:
                voucher = (data.get('risk_extra') or {}).get('v_voucher')
                if voucher:
                    url = ('https://live.bilibili.com/p/html/bilili-page-face-auth/index.html'
                           '?is_live_webview=1&app_common=open&v_voucher=' + quote(str(voucher)))
                    return jsonify({
                        'error': message,
                        'code': code,
                        'face_auth': {
                            'type': 'url',
                            'url': url,
                            'message': '请在新页面完成 B 站验证，然后点“继续开播”。',
                        },
                    }), 400
            raise BiliError(message, code)
        data = value.get('data') or {}
        rtmp = data.get('rtmp') or {}
        srt = data.get('srt') or {}
        if not rtmp.get('addr') or not rtmp.get('code'):
            raise BiliError('B 站没有返回推流码，可能分区不可用或账号无开播权限')
        stream = {
            'rtmp_addr': rtmp.get('addr'),
            'rtmp_code': rtmp.get('code'),
            'srt_addr': srt.get('addr') or '',
            'srt_code': srt.get('code') or '',
            'started_at': time.time(),
        }
        STATE['config'] = {'room_id': room, 'title': title, 'area_v2': area}
        _save_config()
        STATE['stream'] = stream
        _write_private(STREAM_FILE, stream)
        STATE['live'] = None
        return jsonify({'ok': True, 'stream': stream, 'title_warning': title_warning})


@app.post('/api/stop')
def api_stop():
    room = str(STATE['config'].get('room_id') or '').strip()
    if not room.isdigit():
        raise BiliError('缺少房间号')
    if not _logged_in() or not _csrf():
        raise BiliError('请先登录 B 站账号')
    with LOCK:
        csrf = _csrf()
        _json(_bili_post(
            'https://api.live.bilibili.com/room/v1/Room/stopLive',
            {'room_id': room, 'platform': 'pc_link', 'csrf_token': csrf, 'csrf': csrf},
            headers=LIVE_HEADERS))
        STATE['stream'] = None
        STATE['live'] = None
        try:
            STREAM_FILE.unlink()
        except OSError:
            pass
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8090')), threaded=True)
