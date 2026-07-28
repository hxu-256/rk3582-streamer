# rockpi_camera — CLAUDE.md

## Board
Radxa ROCK 5C, RK3582 SoC, 2 GB RAM, kernel 6.1.84-8-rk2410.

## Camera
- Device: `/dev/video-camera0` (symlink to rkisp_mainpath — ISP-processed output)
- Also accessible as `/dev/video11`
- Raw capture devices: `/dev/video0`–`/dev/video10` (rkcif, unprocessed)
- Native output format: **NM12** (Y/UV 4:2:0 non-contiguous multiplanar) — not plain NV12
- Formats negotiable: UYVY, NV16, NV61, NV21, NV12, NM21, NM12 — **no native MJPEG or H.264**
- Resolution: stepwise 32×32 – 3280×2464 in 8-pixel steps — **ISP scaler handles hardware downscale**
- Sensor framerate is fixed; `VIDIOC_G_PARM` fails — do not specify `framerate=` in caps
- **Never specify `format=NV12` in caps** — camera outputs NM12; forcing NV12 inserts a software copy (11.6 MB per frame at full res)

## Hardware encoder (MPP)
- Device: `/dev/mpp_service`
- GStreamer plugin: `libgstrockchipmpp.so` (`gstreamer1.0-rockchip1` package, installed)
- V4L2 wrapper: `libv4l-rkmpp.so` (installed)
- C library: `librockchip_mpp.so.1` (installed)
- The encoder is independent of the GPU — GPU being disabled does not affect it

## CMA (Contiguous Memory Allocator) — critical for MPP
MPP requires CMA for DMA buffer allocation. The kernel default is 8 MB which is fully consumed
by the ISP and display pipeline at boot, leaving 0 for the encoder. Without CMA, MPP leaks every
frame into the heap causing OOM (fast at 4K, slower at 720p but inevitable).

**Fix applied:** `cma=128M` added to `/etc/kernel/cmdline`, regenerated via `sudo u-boot-update`.
- Verify after reboot: `cat /proc/meminfo | grep Cma` → CmaTotal should be 131072 kB
- 128 MB gives ~120 MB free for encoder after ISP/display take their share

## Tested working pipelines (run over SSH only)

**720p H.265 to fakesink (stability test — RAM flat, ~780 MB used):**
```
gst-launch-1.0 -e \
  v4l2src device=/dev/video-camera0 ! \
  video/x-raw,width=1280,height=720 ! \
  mpph265enc bps=4000000 rc-mode=vbr gop=15 ! \
  fakesink sync=false
```

**4K H.265 to fakesink (stable — ~1050 MB used, no leak):**
```
gst-launch-1.0 -e \
  v4l2src device=/dev/video-camera0 ! \
  video/x-raw,width=3280,height=2464 ! \
  mpph265enc bps=8000000 rc-mode=vbr gop=15 ! \
  fakesink sync=false
```

**4K H.264 UDP stream to client:**
```
gst-launch-1.0 -e \
  v4l2src device=/dev/video-camera0 ! \
  video/x-raw,width=3280,height=2464 ! \
  mpph264enc bps=12000000 rc-mode=vbr gop=30 ! \
  h264parse ! \
  rtph264pay config-interval=1 ! \
  udpsink host=<CLIENT_IP> port=5000 sync=false
```

## Critical hardware hazard — DO NOT run from within KDE
Opening `/dev/mpp_service` (e.g. via `gst-inspect-1.0`, `gst-launch-1.0`, or any MPP-using process) triggers a cold hardware initialization of the MPP unit. This resets a shared IOMMU/bus that the display pipeline (VOP) also sits on. With no GPU driver active (llvmpipe/software rendering), kwin cannot survive the disruption and crashes, ending the KDE session.

**Safe rules:**
- Run any GStreamer or MPP commands over SSH, never from a KDE terminal
- The camera web server must run as a systemd service (starts before KDE, MPP already warm by the time desktop loads)
- Never run `gst-inspect-1.0` without clearing the plugin path: `GST_PLUGIN_SYSTEM_PATH='' gst-inspect-1.0 --version`
- Use `dpkg -l gstreamer1.0-rockchip1` to check plugin presence instead of gst-inspect
- Use `/sys/class/video4linux/*/name` to read camera info instead of `v4l2-ctl --list-formats-ext`
- Avoid `find /usr/lib` — large scans cause memory spikes that can destabilise kwin

## Why the GPU is disabled
The Mali GPU on RK3582 has no working open-source driver at this time. X11 falls back to llvmpipe (software rendering via Mesa). This makes the desktop fragile under any hardware disruption.

## Python environment (installed)
- tornado 6.5.7
- pillow 12.2.0
- numpy 2.4.6
- OpenCV: **not installed** (system python — see NPU section below for the conda env that has it)
- Flask/FastAPI: **not installed**

## GStreamer environment (installed)
- GStreamer 1.22.9 with plugins-base, plugins-good, plugins-bad, plugins-ugly, libav
- `gst-launch-1.0` and `gst-inspect-1.0` available at `/usr/bin/`
- Rockchip MPP plugin installed (`gstreamer1.0-rockchip1`)

## Web server plan — implemented
`server.py` runs mediamtx + ffmpeg (direct `-f v4l2` on `/dev/video11` → `h264_rkmpp`/`hevc_rkmpp`
→ RTSP) and a Tornado control UI (snapshot/record) on `:8080`. Viewers get WebRTC (`:8889`), RTSP
(`:8554`), HLS (`:8888`). See below for the NPU-annotated variant (`--npu` flag).

## NPU object detection (RKNN integration)

### What's here
- `npu/` — copied from `~/workspace/rknn-multi-threaded/` (kept as a separate copy, not a
  `sys.path` hack, so the two projects stay independently editable):
  - `rknnpool.py` — threaded `rknnPoolExecutor` wrapping `rknnlite.api.RKNNLite`, one worker
    thread pinned per physical NPU core via `core_mask` (`id % 3`)
  - `func.py` — `myFunc`: resizes a *copy* of the frame to the model's fixed 640×640 input,
    runs inference, then scales detection boxes back onto the native-resolution frame before
    drawing (boxes are linearly rescaled by `orig_w/640`, `orig_h/640` — valid because the
    resize is a forced/non-letterboxed square, no padding to account for)
  - `rknnModel/yolov5s_relu_tk2_RK3588_i8.rknn` — YOLOv5s, ReLU instead of SiLU (small accuracy
    loss for NPU speed), INT8, **static 640×640 input** (model can't accept any other shape).
    Compiled for RK3588 but loads and runs fine on this RK3582 — confirmed empirically, same
    NPU IP block.
- `npu_stream.py` — the actual pipeline: `cv2.VideoCapture("/dev/video11")` → `rknnPoolExecutor`
  (`TPEs=1` default, `--tpes {1,2,3}` to change) → annotated frames piped as raw BGR24 into an
  `ffmpeg -f rawvideo -i - -c:v h264_rkmpp ... -f rtsp` subprocess, publishing to the same
  `rtsp://127.0.0.1:8554/camera` mediamtx already serves. `--width`/`--height` (default
  1280×720) set both the requested capture resolution and the encoded/streamed size — the NPU
  itself is unaffected by this, it always works on its own 640×640 resized copy regardless.
- `server.py --npu` — passes its `--res` profile's WxH through to `npu_stream.py` instead of
  running the plain direct-v4l2 ffmpeg command; mediamtx and the Tornado UI are unchanged.

### Environment
- Conda env **`rknn311`** (`/home/radxa/miniconda3/envs/rknn311/bin/python`) has both `cv2`
  4.13.0 and `rknn_toolkit_lite2`/`rknnlite` 2.3.2 — the only env with both. `server.py`'s
  `--npu` path shells out to this interpreter explicitly (`NPU_PYTHON` constant) since
  `server.py` itself runs under a different env (has tornado, not cv2/rknnlite).
- `ffmpeg` (`/usr/local/bin/ffmpeg`, the `nyanmisaka/ffmpeg-rockchip` build — source still at
  `~/Downloads/ffmpeg-rockchip`) is a system binary usable from any env.

### Hard constraint: single camera consumer
`/dev/video11` only supports one active capture consumer. The NPU path's `cv2.VideoCapture`
must be the *only* thing reading it — never run `server.py`'s plain (non-`--npu`) direct-v4l2
stream at the same time as `npu_stream.py`. This is why the NPU path switches ffmpeg's input
from `-f v4l2 -i /dev/video11` to `-f rawvideo -i -` (reading annotated frames from Python's
stdout via a pipe) instead.

### NPU cores and throughput — camera-bound, not NPU-bound
This board has **3 physical NPU cores**, confirmed by successfully `init_runtime()`-ing
`NPU_CORE_0`, `NPU_CORE_1`, `NPU_CORE_2`, and `NPU_CORE_0_1_2` individually. `TPEs=1` (the
default) uses only one, leaving two idle. **Tested TPEs=1 vs TPEs=3 at 720p: both give the
identical 19.4 fps.** This means the camera's own fixed capture rate (not NPU compute) is the
throughput ceiling — matches the "sensor framerate is fixed" note above. Raising `--tpes` costs
extra RAM (another full `RKNNLite` model context, plus one more in-flight frame buffer each) for
**no fps gain** on this camera. Leave it at 1 unless a future faster camera changes this.

### RAM
- 720p+NPU: peaks ~1.3Gi/1.9Gi used, recovers cleanly to ~900Mi after stop, no swap growth
  during steady operation. Stable.
- 4K+NPU (`--width 3280 --height 2464`): available RAM drops to **~260-310Mi** within ~10-20s
  and plateaus there (doesn't recover during the run) — right at the edge on this 2GB board.
- Two RAM fixes applied in `func.py`/`npu_stream.py` (both real but modest wins, ~2.5% lower
  peak RSS at 720p, bigger relative savings at 4K since they scale with frame size):
  1. `func.py`: resize **before** color-converting (`cv2.cvtColor` is a pure channel
     permutation, no interpolation, so it commutes with resize) — shrinks that copy from
     full-resolution down to 640×640.
  2. `npu_stream.py`: write `out_frame.data` (zero-copy buffer view) to ffmpeg's stdin instead
     of `out_frame.tobytes()` (which forces a full extra copy every frame).

### Known bug: 4K + NPU together corrupts the H.264 stream
Confirmed via direct A/B test, same board, same session:
- **4K, NPU on**: `ffprobe`/playback show repeated decode errors (`error while decoding MB ...,
  bytestream -5/-6/-7`, `left block unavailable for requested intra4x4 mode`). RAM
  available: ~310Mi.
- **4K, NPU off** (plain `server.py` direct-v4l2 → `hevc_rkmpp` path): completely clean, zero
  decode errors, correct 3280×2464/hevc/17fps — **despite similarly tight RAM** (~445Mi
  available). This rules out "4K is just too much for this board" — the non-NPU path handles
  4K fine under comparable memory pressure.
- **720p, NPU on**: no artifacts, TPEs=1 or 3 both clean.

So the corruption is specific to the combination of 4K *and* the NPU pipeline's architecture,
not 4K alone or RAM pressure alone. Leading hypothesis (not fully isolated): the non-NPU path
has ffmpeg own the entire capture→encode loop itself via V4L2 in tightly-timed C; the NPU path
instead round-trips every frame through Python (capture → resize → infer → draw → `write()`
raw bytes into ffmpeg's stdin pipe), and ffmpeg's rawvideo demuxer requires byte-perfect frame
alignment on that pipe with zero tolerance for a hiccup — under 4K's much heavier memory/
scheduling load, something in that Python-mediated hand-off likely desyncs the byte stream.
Candidates not yet tested: the default 64KB pipe buffer forcing many chunked writes per 24MB
frame, GC pauses, or scheduling jitter between the write and ffmpeg's read.

**Practical takeaway: don't run 4K+NPU together.** 720p+NPU is stable and verified; 4K without
NPU is stable and verified; 4K with NPU is the one combination that's both RAM-critical and
visibly corrupting the stream.
