#!/usr/bin/env python3
"""
NPU-annotated camera stream — Radxa ROCK 5C.

Reads /dev/video11 via OpenCV, runs YOLOv5s object detection on the NPU
(rknnPoolExecutor from npu/rknnpool.py, TPEs=1) — the model has a fixed
640x640 input, but func.py resizes only a copy for inference and remaps
boxes back onto the native-resolution frame — and pipes the annotated
frames into ffmpeg's hardware h264_rkmpp encoder, publishing to the same
rtsp://127.0.0.1:8554/camera URL that mediamtx already serves as
WebRTC/RTSP/HLS.

/dev/video11 only supports one capture consumer at a time, so this script's
cv2.VideoCapture must be the only thing reading the camera — do not also run
server.py's plain (non --npu) ffmpeg-v4l2 stream at the same time.

4K capture is untested/risky on this 2GB board: every frame gets several
full-resolution Python-side copies (color convert, resize, draw, tobytes),
unlike the pure-GStreamer 4K pipeline documented as stable in CLAUDE.md.

NEVER run from a KDE terminal — over SSH or as a systemd service only.
"""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

import cv2

from npu.rknnpool import rknnPoolExecutor
from npu.func import myFunc

DEVICE     = '/dev/video11'
MODEL_PATH = str(Path(__file__).parent / 'npu' / 'rknnModel' / 'yolov5s_relu_tk2_RK3588_i8.rknn')

_cap = None
_pool = None
_proc = None


def build_ffmpeg(rtsp_url, bitrate, fps, width, height):
    return subprocess.Popen([
        'ffmpeg',
        '-f', 'rawvideo', '-pix_fmt', 'bgr24',
        '-s', f'{width}x{height}',
        '-use_wallclock_as_timestamps', '1', '-r', str(fps),
        '-i', '-',
        '-c:v', 'h264_rkmpp', '-b:v', bitrate, '-g', '30', '-bf', '0',
        '-f', 'rtsp', rtsp_url,
        '-loglevel', 'warning',
    ], stdin=subprocess.PIPE)


def cleanup(*_):
    print('\n[npu_stream] shutting down...')
    if _cap is not None:
        _cap.release()
    if _pool is not None:
        _pool.release()
    if _proc is not None and _proc.stdin:
        try:
            _proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        _proc.wait()
    sys.exit(0)


def main():
    global _cap, _pool, _proc

    ap = argparse.ArgumentParser(description='NPU-annotated camera stream')
    ap.add_argument('--rtsp', default='rtsp://127.0.0.1:8554/camera',
                     help='RTSP URL to publish to (mediamtx must already be running)')
    ap.add_argument('--bitrate', default='4M', help='h264_rkmpp target bitrate')
    ap.add_argument('--fps', type=int, default=15,
                     help='nominal frame rate hint for ffmpeg (actual PTS come from wallclock)')
    ap.add_argument('--width', type=int, default=1280, help='requested capture width')
    ap.add_argument('--height', type=int, default=720, help='requested capture height')
    args = ap.parse_args()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    _cap = cv2.VideoCapture(DEVICE)
    if not _cap.isOpened():
        print(f'[npu_stream] failed to open {DEVICE}')
        sys.exit(1)

    _cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    _cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    _pool = rknnPoolExecutor(rknnModel=MODEL_PATH, TPEs=1, func=myFunc)

    # prime the pool with one in-flight frame (TPEs=1 -> TPEs+1 frames queued)
    width = height = None
    for _ in range(2):
        ret, frame = _cap.read()
        if not ret:
            print('[npu_stream] failed to read initial frame')
            cleanup()
        height, width = frame.shape[:2]
        _pool.put(frame)

    if (width, height) != (args.width, args.height):
        print(f'[npu_stream] warning: requested {args.width}x{args.height}, '
              f'camera actually negotiated {width}x{height} — using that')

    _proc = build_ffmpeg(args.rtsp, args.bitrate, args.fps, width, height)
    print(f'[npu_stream] publishing {width}x{height} to {args.rtsp}')

    frames, loop_t, init_t = 0, time.time(), time.time()
    while _cap.isOpened():
        ret, frame = _cap.read()
        if not ret:
            break
        _pool.put(frame)
        out_frame, ok = _pool.get()
        if not ok:
            break
        try:
            _proc.stdin.write(out_frame.data)  # zero-copy buffer view, no .tobytes() copy
        except BrokenPipeError:
            print('[npu_stream] ffmpeg pipe closed, stopping')
            break

        frames += 1
        if frames % 30 == 0:
            fps = 30 / (time.time() - loop_t)
            print(f'[npu_stream] {fps:.1f} fps')
            loop_t = time.time()

    print('[npu_stream] avg fps', frames / (time.time() - init_t))
    cleanup()


if __name__ == '__main__':
    main()
