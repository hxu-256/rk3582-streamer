#!/usr/bin/env python3
"""
Camera web server — Radxa ROCK 5C
Starts mediamtx + ffmpeg-rockchip, serves a control UI on port 8080.

Usage:  python3 server.py [--res 720p|4k] [--port 8080]
NEVER run from a KDE terminal — use SSH or systemd.
"""

import argparse
import asyncio
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import tornado.ioloop
import tornado.web

BASE     = Path(__file__).parent
SNAPS    = BASE / 'snapshots'
RECS     = BASE / 'recordings'
MEDIAMTX = '/home/radxa/Downloads/mediamtx/mediamtx'
MTX_CONF = '/home/radxa/Downloads/mediamtx/mediamtx.yml'
DEVICE   = '/dev/video11'

PROFILES = {
    '720p': ('1280x720',  'h264_rkmpp', '4M'),
    '4k':   ('3280x2464', 'hevc_rkmpp', '40M'),
}

for d in (SNAPS, RECS):
    d.mkdir(exist_ok=True)

# ── process handles ──────────────────────────────────────────────────────────
_mtx      = None
_cam      = None
_rec      = None
_rec_path = None
_res      = '720p'


def _local_ip():
    return subprocess.check_output(
        "ip route get 1 | awk '{print $7; exit}'", shell=True
    ).decode().strip()


def _kill(proc):
    if proc and proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass


def launch_mediamtx():
    global _mtx
    _mtx = subprocess.Popen(
        [MEDIAMTX, MTX_CONF],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    print(f'[mtx] started PID {_mtx.pid}')


def launch_stream(res):
    global _cam, _res
    _res = res
    size, codec, bitrate = PROFILES[res]
    _cam = subprocess.Popen([
        'ffmpeg',
        '-f', 'v4l2', '-use_wallclock_as_timestamps', '1', '-r', '17',
        '-input_format', 'nv12', '-video_size', size, '-i', DEVICE,
        '-c:v', codec, '-b:v', bitrate, '-g', '30', '-bf', '0',
        '-f', 'rtsp', 'rtsp://127.0.0.1:8554/camera',
        '-loglevel', 'warning',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f'[cam] {res} stream started PID {_cam.pid}')


def do_snapshot():
    ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = SNAPS / f'snap_{ts}.jpg'
    r = subprocess.run([
        'ffmpeg', '-rtsp_transport', 'tcp',
        '-i', 'rtsp://127.0.0.1:8554/camera',
        '-vframes', '1', '-q:v', '2', str(path), '-y',
        '-loglevel', 'warning',
    ], timeout=10)
    return path.name if r.returncode == 0 and path.exists() else None


def do_record_start():
    global _rec, _rec_path
    if _rec and _rec.poll() is None:
        return None
    ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
    _rec_path = RECS / f'rec_{ts}.mp4'
    _rec      = subprocess.Popen([
        'ffmpeg', '-rtsp_transport', 'tcp',
        '-i', 'rtsp://127.0.0.1:8554/camera',
        '-c', 'copy', str(_rec_path),
        '-loglevel', 'warning',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f'[rec] started → {_rec_path.name}')
    return _rec_path.name


def do_record_stop():
    global _rec
    name = _rec_path.name if _rec_path else None
    _kill(_rec)
    _rec = None
    print(f'[rec] stopped → {name}')
    return name


def shutdown():
    _kill(_rec)
    _kill(_cam)
    _kill(_mtx)


# ── HTML ─────────────────────────────────────────────────────────────────────
def _make_html(ip, res):
    res_label = '720p H.264' if res == '720p' else '4K H.265'
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Camera — {ip}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #ccc; font-family: monospace;
      max-width: 960px; margin: 0 auto; padding: 24px 16px;
    }}
    h1 {{ color: #fff; font-size: 15px; margin-bottom: 16px; }}
    h2 {{ font-size: 12px; color: #555; margin: 20px 0 6px; text-transform: uppercase; letter-spacing: .05em; }}
    video {{
      width: 100%; aspect-ratio: 16/9; background: #111;
      border: 1px solid #222; display: block;
    }}
    #status {{ font-size: 11px; color: #555; margin: 5px 0 14px; }}
    #status.live {{ color: #4b4; }}
    #status.error {{ color: #b44; }}
    .controls {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }}
    button {{
      background: #1a1a1a; border: 1px solid #333; color: #bbb;
      font-family: monospace; font-size: 13px;
      padding: 8px 20px; cursor: pointer; border-radius: 3px;
      transition: background .15s;
    }}
    button:hover:not(:disabled) {{ background: #252525; border-color: #555; }}
    button:disabled {{ opacity: 0.35; cursor: default; }}
    button.rec-active {{ border-color: #833; color: #f88; }}
    #snap-preview {{ max-width: 100%; margin-top: 8px; display: none; border: 1px solid #222; }}
    #snap-dl {{ display: none; font-size: 12px; color: #7ad; margin-top: 5px; }}
    .file-list {{ list-style: none; }}
    .file-list li {{
      font-size: 12px; padding: 6px 0; border-bottom: 1px solid #1a1a1a;
      display: flex; justify-content: space-between; align-items: center;
    }}
    .file-list span {{ color: #888; }}
    .file-list a {{ color: #7ad; text-decoration: none; }}
    .file-list a:hover {{ text-decoration: underline; }}
    .empty {{ color: #333; font-size: 12px; padding: 6px 0; }}
    .dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%;
            background: #b44; margin-right: 6px; animation: blink 1s step-start infinite; }}
    @keyframes blink {{ 50% {{ opacity: 0; }} }}
  </style>
</head>
<body>
  <h1>Camera &mdash; {res_label}</h1>

  <video id="video" autoplay playsinline muted></video>
  <div id="status">connecting&hellip;</div>

  <div class="controls">
    <button id="snap-btn" onclick="takeSnapshot()">Snapshot</button>
    <button id="rec-btn"  onclick="toggleRecord()">Start recording</button>
  </div>

  <h2>Latest snapshot</h2>
  <img id="snap-preview">
  <a  id="snap-dl"></a>

  <h2>Recordings</h2>
  <ul id="rec-list" class="file-list"></ul>

  <h2>All snapshots</h2>
  <ul id="snap-list" class="file-list"></ul>

<script>
const video  = document.getElementById('video');
const status = document.getElementById('status');
let recording = false;

async function startStream() {{
  const pc = new RTCPeerConnection();
  pc.ontrack = e => {{ video.srcObject = e.streams[0]; }};
  pc.addTransceiver('video', {{direction: 'recvonly'}});

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  await new Promise(r => {{
    if (pc.iceGatheringState === 'complete') return r();
    pc.onicegatheringstatechange = () => {{ if (pc.iceGatheringState === 'complete') r(); }};
    setTimeout(r, 3000);
  }});

  try {{
    const resp = await fetch('http://{ip}:8889/camera/whep', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/sdp'}},
      body: pc.localDescription.sdp,
    }});
    if (!resp.ok) throw new Error(resp.status);
    await pc.setRemoteDescription({{type: 'answer', sdp: await resp.text()}});
    status.textContent = 'live — {res_label}';
    status.className = 'live';
  }} catch(e) {{
    status.textContent = 'stream not ready, retrying…';
    status.className = 'error';
    setTimeout(startStream, 3000);
  }}
}}

async function takeSnapshot() {{
  const btn = document.getElementById('snap-btn');
  btn.disabled = true;
  btn.textContent = 'capturing…';
  try {{
    const r = await fetch('/api/snapshot', {{method: 'POST'}});
    const j = await r.json();
    if (j.ok) {{
      const img = document.getElementById('snap-preview');
      img.src = j.url + '?t=' + Date.now();
      img.style.display = 'block';
      const dl = document.getElementById('snap-dl');
      dl.href = j.url; dl.download = j.file;
      dl.textContent = '↓ ' + j.file;
      dl.style.display = 'block';
      loadFiles();
    }}
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Snapshot';
  }}
}}

async function toggleRecord() {{
  const btn = document.getElementById('rec-btn');
  if (!recording) {{
    const r = await fetch('/api/record/start', {{method: 'POST'}});
    const j = await r.json();
    if (j.ok) {{
      recording = true;
      btn.innerHTML = '<span class="dot"></span>Stop recording';
      btn.classList.add('rec-active');
    }}
  }} else {{
    await fetch('/api/record/stop', {{method: 'POST'}});
    recording = false;
    btn.textContent = 'Start recording';
    btn.classList.remove('rec-active');
    loadFiles();
  }}
}}

async function loadFiles() {{
  const r = await fetch('/api/files');
  const j = await r.json();
  const fmt = (files, path) => files.length
    ? files.map(f => `<li><span>${{f.name}}</span><a href="${{f.url}}" download>download</a></li>`).join('')
    : '<li class="empty">none yet</li>';
  document.getElementById('rec-list').innerHTML  = fmt(j.recordings);
  document.getElementById('snap-list').innerHTML = fmt(j.snapshots);
}}

startStream();
loadFiles();
</script>
</body>
</html>"""


# ── handlers ─────────────────────────────────────────────────────────────────
class IndexHandler(tornado.web.RequestHandler):
    def initialize(self, html):
        self._html = html

    def get(self):
        self.set_header('Content-Type', 'text/html')
        self.write(self._html)


class SnapshotHandler(tornado.web.RequestHandler):
    async def post(self):
        loop = asyncio.get_event_loop()
        name = await loop.run_in_executor(None, do_snapshot)
        if name:
            self.write({'ok': True, 'file': name, 'url': f'/snapshots/{name}'})
        else:
            self.set_status(500)
            self.write({'ok': False, 'error': 'capture failed — is stream running?'})


class RecordStartHandler(tornado.web.RequestHandler):
    def post(self):
        name = do_record_start()
        if name:
            self.write({'ok': True, 'file': name})
        else:
            self.set_status(409)
            self.write({'ok': False, 'error': 'already recording'})


class RecordStopHandler(tornado.web.RequestHandler):
    def post(self):
        name = do_record_stop()
        if name:
            self.write({'ok': True, 'file': name, 'url': f'/recordings/{name}'})
        else:
            self.write({'ok': False, 'error': 'not recording'})


class FilesHandler(tornado.web.RequestHandler):
    def get(self):
        snaps = sorted(SNAPS.glob('*.jpg'), reverse=True)
        recs  = sorted(RECS.glob('*.mp4'),  reverse=True)
        self.write({
            'snapshots':  [{'name': f.name, 'url': f'/snapshots/{f.name}'} for f in snaps[:10]],
            'recordings': [{'name': f.name, 'url': f'/recordings/{f.name}'} for f in recs[:10]],
        })


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='Rockchip camera web server')
    ap.add_argument('--res',  default='720p', choices=PROFILES, help='stream resolution')
    ap.add_argument('--port', type=int, default=8080, help='web UI port')
    args = ap.parse_args()

    print('[init] starting mediamtx…')
    launch_mediamtx()
    print(f'[init] starting {args.res} stream…')
    launch_stream(args.res)

    ip   = _local_ip()
    html = _make_html(ip, args.res)

    app = tornado.web.Application([
        (r'/',                 IndexHandler,                    {'html': html}),
        (r'/api/snapshot',     SnapshotHandler),
        (r'/api/record/start', RecordStartHandler),
        (r'/api/record/stop',  RecordStopHandler),
        (r'/api/files',        FilesHandler),
        (r'/snapshots/(.*)',   tornado.web.StaticFileHandler,   {'path': str(SNAPS)}),
        (r'/recordings/(.*)',  tornado.web.StaticFileHandler,   {'path': str(RECS)}),
    ])
    app.listen(args.port)

    print(f'[server] http://{ip}:{args.port}/')
    print(f'[server] RTSP  rtsp://{ip}:8554/camera')

    def _sig(s, f):
        print('\n[server] shutting down…')
        shutdown()
        tornado.ioloop.IOLoop.current().add_callback_from_signal(
            tornado.ioloop.IOLoop.current().stop
        )

    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    tornado.ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
