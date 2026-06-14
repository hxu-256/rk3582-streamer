#!/usr/bin/env python3
"""
Camera web server — Radxa ROCK 5C
H.265 hardware encode via Rockchip MPP → MPEG-TS over TCP

Camera runs at ~17 fps fixed (sensor clock), ISP scaler handles resolution.
Default: 1280x720, 4 Mbps VBR → ~14 fps after encoder.

Endpoints:
  http://<ip>:8080/      Info page with connection instructions
  tcp://<ip>:8554        H.265 MPEG-TS stream

Connect with:
  ffplay -fflags nobuffer -flags low_delay -framedrop tcp://<ip>:8554
  vlc tcp://<ip>:8554

Run over SSH or as a systemd service.
NEVER from a KDE terminal — MPP cold-init disrupts the display pipeline.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import tornado.web
import tornado.ioloop


def make_index_html(ip, tcp_port):
    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Camera — {ip}</title>
  <style>
    body {{ background: #111; color: #ccc; font-family: monospace;
           max-width: 600px; margin: 60px auto; padding: 0 20px; }}
    h1 {{ color: #fff; font-size: 18px; }}
    code {{ display: block; background: #222; padding: 10px 14px;
            border-radius: 4px; margin: 8px 0; color: #7df; font-size: 14px; }}
    p {{ color: #888; font-size: 13px; }}
  </style>
</head>
<body>
  <h1>Camera stream</h1>
  <p>H.265 hardware encoded, MPEG-TS over TCP.</p>
  <p><b>ffplay</b> (low latency):</p>
  <code>ffplay -fflags nobuffer -flags low_delay -framedrop tcp://{ip}:{tcp_port}</code>
  <p><b>VLC</b>:</p>
  <code>vlc tcp://{ip}:{tcp_port}</code>
  <p><b>ffmpeg record</b>:</p>
  <code>ffmpeg -i tcp://{ip}:{tcp_port} -c copy output.mkv</code>
</body>
</html>
"""


def build_pipeline(device, width, height, bps, tcp_port):
    return [
        'gst-launch-1.0', '-e', '-q',
        # No format= in caps: camera outputs NM12 (non-contiguous), not NV12.
        # Specifying format=NV12 forces a software copy of every frame.
        'v4l2src', f'device={device}', '!',
        f'video/x-raw,width={width},height={height}', '!',
        'mpph265enc', f'bps={bps}', 'rc-mode=vbr', 'gop=30', '!',
        'h265parse', 'config-interval=-1', '!',
        'mpegtsmux', '!',
        'tcpserversink', f'port={tcp_port}', 'host=0.0.0.0',
        'recover-policy=keyframe', 'sync-method=latest-keyframe',
        'sync=false',
    ]


class GStreamerRunner(threading.Thread):
    def __init__(self, cmd):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.proc = None
        self.started = threading.Event()

    def run(self):
        print(f'[gst] Starting: {" ".join(self.cmd[:5])} ...', flush=True)
        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # signal ready once we see the pipeline playing
        def watch_stderr():
            for line in self.proc.stderr:
                txt = line.decode().rstrip()
                if txt:
                    print(f'[gst] {txt}', flush=True)
                if b'PLAYING' in line or b'New clock' in line:
                    self.started.set()
        t = threading.Thread(target=watch_stderr, daemon=True)
        t.start()
        self.proc.wait()
        self.started.set()   # unblock wait_until_ready if pipeline failed
        print('[gst] Pipeline exited', flush=True)

    def wait_until_ready(self, timeout=15):
        return self.started.wait(timeout=timeout)

    def is_alive_proc(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class IndexHandler(tornado.web.RequestHandler):
    def initialize(self, html):
        self.html = html

    def get(self):
        self.set_header('Content-Type', 'text/html')
        self.write(self.html)


def main():
    parser = argparse.ArgumentParser(description='Rockchip H.265 camera server')
    parser.add_argument('--device', default='/dev/video11')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--bps', type=int, default=4_000_000,
                        help='H.265 bitrate in bps (default 4 Mbps)')
    parser.add_argument('--port', type=int, default=8080,
                        help='HTTP info page port (default 8080)')
    parser.add_argument('--tcp-port', type=int, default=8554,
                        help='TCP port for H.265 stream (default 8554)')
    args = parser.parse_args()

    cmd = build_pipeline(args.device, args.width, args.height,
                         args.bps, args.tcp_port)

    gst = GStreamerRunner(cmd)
    gst.start()

    print('[server] Waiting for pipeline to start...', flush=True)
    gst.wait_until_ready(timeout=15)

    if not gst.is_alive_proc():
        print('[server] ERROR: pipeline failed to start. See logs above.', flush=True)
        sys.exit(1)

    ip = subprocess.check_output(
        "ip route get 1 | awk '{print $7; exit}'", shell=True
    ).decode().strip()

    html = make_index_html(ip, args.tcp_port)
    app = tornado.web.Application([
        (r'/', IndexHandler, {'html': html}),
    ])
    app.listen(args.port)

    print(f'[server] Info page : http://{ip}:{args.port}/', flush=True)
    print(f'[server] H.265 TCP : tcp://{ip}:{args.tcp_port}', flush=True)
    print(f'[server] ffplay cmd: ffplay -fflags nobuffer -flags low_delay '
          f'-framedrop tcp://{ip}:{args.tcp_port}', flush=True)

    def shutdown(sig, frame):
        print('\n[server] Shutting down...', flush=True)
        gst.stop()
        tornado.ioloop.IOLoop.current().stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    tornado.ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
