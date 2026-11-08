import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from voice_lines import LINES

DATA = Path(os.environ.get('STREAM_DATA_DIR', './data'))
ENV = Path(os.environ.get('BREEZE_ENV', str(DATA / 'breeze.env')))
VOICE_DIR = Path(os.environ.get('STREAM_VOICE_DIR', str(DATA / 'voice')))


def load_env():
    values = {}
    for line in ENV.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            name, value = line.split('=', 1)
            values[name.strip()] = value.strip()
    return values


def synthesize(values, text, instructions):
    url = ('https://api.breeze.blue/v1/text-to-speech/%s?output_format=wav_24000'
           % values['BREEZE_VOICE_ID'])
    payload = {
        'text': text,
        'model_id': values.get('BREEZE_MODEL_ID', 'breeze-tts-2'),
        'language_code': 'zh',
        'instructions': instructions,
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'xi-api-key': values['BREEZE_API_KEY'],
                 'Content-Type': 'application/json',
                 'Accept': 'audio/wav',
                 'User-Agent': 'bili-live-code-voice-render'},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, response.headers.get('Content-Type', ''), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get('Content-Type', ''), error.read()


def main():
    values = load_env()
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(VOICE_DIR, 0o700)
    force = '--force' in sys.argv
    generated = 0
    for name, (text, instructions) in LINES.items():
        path = VOICE_DIR / (name + '.wav')
        if path.exists() and path.stat().st_size > 0 and not force:
            print('skip', name)
            continue
        started = time.time()
        status, ctype, body = synthesize(values, text, instructions)
        elapsed = time.time() - started
        if status != 200 or not body.startswith(b'RIFF'):
            preview = body[:200].decode('utf-8', 'replace') if body else ''
            print('failed', name, status, ctype, preview)
            continue
        path.write_bytes(body)
        with wave.open(str(path), 'rb') as handle:
            seconds = handle.getnframes() / float(handle.getframerate())
        print('rendered', name, len(body), 'bytes', round(seconds, 2), 's',
              round(elapsed, 2), 's latency')
        generated += 1
        time.sleep(0.4)
    print('done, generated', generated, 'files in', VOICE_DIR)


if __name__ == '__main__':
    main()
