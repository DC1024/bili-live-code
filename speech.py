"""Voice playback engine for the live stream.

Fixed triggers play pre-rendered WAV lines. Only live-room replies use the
Breeze TTS API, and those are cached by text hash.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from voice_lines import LINES

RATE = 24000
CHUNK_MS = 20
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000
SILENCE = bytes(CHUNK_BYTES)


class SpeechEngine:
    def __init__(self, data_dir, voice_dir, env_file, log=print):
        self.data_dir = Path(data_dir)
        self.voice_dir = Path(voice_dir)
        self.cache_dir = self.voice_dir / 'cache'
        self.fifo = self.data_dir / 'audio.fifo'
        self.env_file = Path(env_file)
        self.log = log
        self.values = self._read_env()
        self.dynamic = bool(self.values.get('BREEZE_API_KEY') and self.values.get('BREEZE_VOICE_ID'))
        self.pending = queue.Queue(maxsize=6)
        self.segments = queue.Queue(maxsize=6)
        self.stop_event = threading.Event()
        self.level = 0.0
        self.speaking = False
        self.current_text = ''
        self._current = b''
        self._current_text = ''
        self._writer = None
        self._synth = None
        self._reader_fd = None

    def _read_env(self):
        values = {}
        try:
            for line in self.env_file.read_text(encoding='utf-8').splitlines():
                if '=' in line and not line.strip().startswith('#'):
                    name, value = line.split('=', 1)
                    values[name.strip()] = value.strip()
        except OSError:
            pass
        return values

    def available(self):
        return self.dynamic

    def state(self):
        return {'level': self.level, 'speaking': self.speaking, 'text': self.current_text}

    def can_accept(self):
        return self.pending.qsize() < 3 and self.segments.qsize() < 4

    def start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass
        if not self.fifo.exists():
            try:
                os.mkfifo(str(self.fifo), 0o600)
            except FileExistsError:
                pass
        try:
            self._reader_fd = os.open(str(self.fifo), os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self._reader_fd = None
        self.stop_event.clear()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True, name='speech-writer')
        self._writer.start()
        if self.dynamic:
            self._synth = threading.Thread(target=self._synth_loop, daemon=True, name='speech-synth')
            self._synth.start()

    def stop(self):
        self.stop_event.set()
        for thread in (self._writer, self._synth):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        if self._reader_fd is not None:
            try:
                os.close(self._reader_fd)
            except OSError:
                pass
            self._reader_fd = None
        try:
            self.fifo.unlink()
        except OSError:
            pass

    def say_line(self, name, text=None):
        path = self.voice_dir / (name + '.wav')
        if not path.exists():
            return False
        pcm = self._wav_pcm(path)
        if not pcm:
            return False
        caption = text or LINES.get(name, (name, ''))[0]
        self._push_segment(pcm, caption)
        return True

    def say_text(self, text, caption=None):
        text = (text or '').strip()
        if not text:
            return False
        if not self.dynamic:
            return False
        cache = self._cache_path(text)
        if cache.exists():
            pcm = self._wav_pcm(cache)
            if pcm:
                self._push_segment(pcm, caption or text)
                return True
        try:
            self.pending.put_nowait((text, caption or text))
            return True
        except queue.Full:
            return False

    def _push_segment(self, pcm, text):
        try:
            self.segments.put_nowait((pcm, text))
        except queue.Full:
            try:
                self.segments.get_nowait()
            except queue.Empty:
                pass
            try:
                self.segments.put_nowait((pcm, text))
            except queue.Full:
                pass

    def _cache_path(self, text):
        digest = hashlib.sha1(text.encode('utf-8')).hexdigest()
        return self.cache_dir / (digest + '.wav')

    def _wav_pcm(self, path):
        try:
            with wave.open(str(path), 'rb') as handle:
                if (handle.getframerate() == RATE and handle.getnchannels() == 1
                        and handle.getsampwidth() == 2):
                    return handle.readframes(handle.getnframes())
        except (OSError, wave.Error):
            pass
        try:
            proc = subprocess.run(
                ['/usr/bin/ffmpeg', '-v', 'error', '-i', str(path),
                 '-f', 's16le', '-ac', '1', '-ar', str(RATE), '-'],
                capture_output=True, timeout=40)
            return proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            return b''

    def _tts(self, text):
        url = ('https://api.breeze.blue/v1/text-to-speech/%s?output_format=wav_24000'
               % self.values['BREEZE_VOICE_ID'])
        payload = {
            'text': text,
            'model_id': self.values.get('BREEZE_MODEL_ID', 'breeze-tts-2'),
            'language_code': 'zh',
        }
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'xi-api-key': self.values['BREEZE_API_KEY'],
                     'Content-Type': 'application/json',
                     'Accept': 'audio/wav',
                     'User-Agent': 'bili-live-code-speech'},
            method='POST')
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
            if not body.startswith(b'RIFF'):
                self.log('tts returned non-wav content')
                return b''
            path = self._cache_path(text)
            path.write_bytes(body)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return self._wav_pcm(path)
        except urllib.error.HTTPError as error:
            self.log('tts http %s' % error.code)
        except urllib.error.URLError as error:
            self.log('tts network error: ' + str(error.reason)[:120])
        except Exception as error:
            self.log('tts failed: ' + str(error)[:120])
        return b''

    def _synth_loop(self):
        while not self.stop_event.is_set():
            try:
                text, caption = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            pcm = self._tts(text)
            if pcm:
                self._push_segment(pcm, caption)

    def _writer_loop(self):
        while not self.stop_event.is_set():
            try:
                fd = os.open(str(self.fifo), os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno in (errno.ENXIO, errno.EAGAIN, errno.ENOENT):
                    time.sleep(0.1)
                    continue
                self.log('audio fifo open failed: ' + str(error)[:120])
                time.sleep(0.5)
                continue
            try:
                os.set_blocking(fd, True)
            except (OSError, AttributeError):
                pass
            next_at = time.monotonic()
            try:
                while not self.stop_event.is_set():
                    chunk, caption, voiced = self._next_chunk()
                    try:
                        os.write(fd, chunk)
                    except (BrokenPipeError, OSError):
                        break
                    self._update_state(chunk, caption, voiced)
                    next_at += CHUNK_MS / 1000.0
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_at = time.monotonic()
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _next_chunk(self):
        if not self._current:
            try:
                self._current, self._current_text = self.segments.get_nowait()
            except queue.Empty:
                return SILENCE, '', False
        chunk = self._current[:CHUNK_BYTES]
        self._current = self._current[CHUNK_BYTES:]
        if len(chunk) < CHUNK_BYTES:
            chunk = chunk + bytes(CHUNK_BYTES - len(chunk))
        return chunk, self._current_text, True

    def _update_state(self, chunk, caption, voiced):
        if not voiced:
            self.level *= 0.85
            self.speaking = False
            self.current_text = ''
            return
        peak = 0
        for index in range(0, len(chunk), 2):
            value = abs(int.from_bytes(chunk[index:index + 2], 'little', signed=True))
            if value > peak:
                peak = value
        self.level = min(1.0, peak / 32768.0 * 3.0)
        self.speaking = True
        self.current_text = caption
