# rockpi_camera

Hardware-encoded camera streaming with optional on-device YOLOv5 object detection for the
**Radxa ROCK 5C (Rockchip RK3582, 2 GB RAM)**.

- ISP camera → Rockchip MPP hardware encoder (H.264 / H.265) → [mediamtx](https://github.com/bluenviron/mediamtx)
- View in a browser (WebRTC), VLC (RTSP), or HLS
- Small Tornado control page for snapshots and recording
- `--npu` mode: YOLOv5s on the RK3582's NPU, boxes drawn onto the live stream

Most of the work here was figuring out undocumented board behaviour. The
[hard-won notes](#hard-won-notes) section is probably the most useful part if you have an
RK3582/RK3588 board.

## Requirements

| Component | Notes |
|---|---|
| Kernel `cma=128M` | **Required** — see [CMA](#cma-the-encoder-leaks-without-it) below |
| [ffmpeg-rockchip](https://github.com/nyanmisaka/ffmpeg-rockchip) | Build with `--enable-rkmpp`; provides `h264_rkmpp` / `hevc_rkmpp`. Must be the `ffmpeg` on `PATH` |
| [mediamtx](https://github.com/bluenviron/mediamtx/releases) | Tested with v1.19.1. Put on `PATH` or set `MEDIAMTX=` |
| Python 3 + `tornado` | For `server.py` |
| `opencv-python` + `rknn-toolkit-lite2` | Only for `--npu` (tested: cv2 4.13, rknnlite 2.3.2, Python 3.11) |

## Usage

**Run over SSH, not from a desktop terminal** (see [the KDE hazard](#opening-the-encoder-can-kill-the-desktop)).

```bash
python3 server.py                 # 720p H.264, UI on :8080
python3 server.py --res 4k        # 3280x2464 H.265
python3 server.py --npu           # 720p with YOLOv5 boxes
./stream.sh [720p|4k]             # stream only, no UI
```

Viewers:

| Protocol | URL | Notes |
|---|---|---|
| WebRTC | `http://<ip>:8889/camera` | Browser. H.264 only — browsers don't do H.265 over WebRTC |
| RTSP | `rtsp://<ip>:8554/camera` | VLC / ffplay, works for both codecs |
| HLS | `http://<ip>:8888/camera` | Browser, higher latency |
| UI | `http://<ip>:8080` | Snapshot / record controls |

Environment overrides:

| Variable | Default |
|---|---|
| `MEDIAMTX` | `mediamtx` on `PATH` |
| `MTX_CONF` | `./mediamtx.yml` |
| `CAMERA_DEVICE` | `/dev/video11` |
| `NPU_PYTHON` | the interpreter running `server.py` — set this if cv2/rknnlite live in a different env (e.g. a conda env) |

## Layout

```
server.py        mediamtx + ffmpeg launcher, Tornado control UI
stream.sh        bare-bones stream launcher
npu_stream.py    cv2 capture → NPU inference → raw BGR into ffmpeg (h264_rkmpp) → RTSP
npu/             threaded RKNNLite pool + YOLOv5 post-processing, YOLOv5s INT8 model
mediamtx.yml     minimal config (defaults + accept any path)
archive/         earlier experiments (GStreamer/MPEG-TS server, cv2 viewer)
```

## Hard-won notes

### CMA: the encoder leaks without it
MPP allocates DMA buffers from CMA. The kernel default (8 MB) is fully consumed by the ISP and
display at boot. With no CMA left, MPP silently falls back in a way that leaks every frame
into normal memory → OOM (within seconds at 4K, minutes at 720p).

```bash
# Radxa images: add to /etc/kernel/cmdline, then
sudo u-boot-update && sudo reboot
grep Cma /proc/meminfo   # CmaTotal: 131072 kB
```

With `cma=128M`, 4K H.265 encode runs indefinitely at ~1050 MB system RAM, flat.

### Opening the encoder can kill the desktop
On these boards the Mali GPU has no working open-source driver, so the desktop runs on llvmpipe.
The first process to open `/dev/mpp_service` triggers a cold MPP init that disturbs an IOMMU/bus
shared with the display controller (VOP), and **kwin crashes, ending the KDE session**. This
includes innocent things like `gst-inspect-1.0` (which loads the rockchip plugin).

- Run encoder-using commands over SSH, or from a systemd service that starts before the desktop.
- `GST_PLUGIN_SYSTEM_PATH='' gst-inspect-1.0 --version` is safe; plain `gst-inspect-1.0` isn't.

### Camera format: NM12, not NV12
`rkisp_mainpath` (`/dev/video11`) natively emits **NM12** (multiplanar NV12). Forcing
`format=NV12` in GStreamer caps inserts a software copy, 11.6 MB per frame at full res. Leave
the format unspecified and let MPP take NM12 directly. Other notes:

- The ISP scaler does hardware downscaling: any size 32×32 to 3280×2464 in 8-px steps.
- The sensor frame rate is fixed (~17-19 fps). `VIDIOC_G_PARM` fails, so don't put `framerate=` in caps.
- No native MJPEG/H.264 from the camera. All compression goes through MPP.
- Only **one** process can capture from the device at a time.

### NPU: RK3588 models run on RK3582, and the camera is the bottleneck
- The `*_RK3588_i8.rknn` model loads and runs unchanged on the RK3582 (same NPU IP).
- All **3 NPU cores** initialise (`NPU_CORE_0/1/2` and `NPU_CORE_0_1_2`).
- 1 worker vs 3 workers at 720p: **identical 19.4 fps**. Capture rate is the limit, not
  inference. Extra workers only cost RAM, so `npu_stream.py` uses `TPEs=1`.
- RAM at 720p+NPU peaks around 1.3 / 1.9 GiB and is stable.

### Known bug: 4K + NPU corrupts the H.264 stream
- 4K **with** NPU: repeated decoder errors (`error while decoding MB`, `bytestream -5/-6/-7`).
- 4K **without** NPU (ffmpeg reading V4L2 directly): clean, under similar memory pressure.
- 720p **with** NPU: clean.

So it isn't RAM pressure alone. The suspect is the NPU path's Python → `ffmpeg -f rawvideo -i -`
pipe: a 24 MB frame goes through a 64 KB pipe in many chunks, and rawvideo has zero tolerance
for misalignment. Untested candidates: pipe buffer size (`F_SETPIPE_SZ`), GC pauses, scheduling
jitter. **Use 720p for NPU.** PRs welcome.

## Credits

- `npu/rknnpool.py`, `npu/func.py` and the YOLOv5s model are adapted from
  [leafqycc/rknn-multi-threaded](https://github.com/leafqycc/rknn-multi-threaded) (Apache-2.0,
  see `npu/LICENSE`). Modified to remap boxes to native resolution and to reduce per-frame copies.
- [nyanmisaka/ffmpeg-rockchip](https://github.com/nyanmisaka/ffmpeg-rockchip),
  [bluenviron/mediamtx](https://github.com/bluenviron/mediamtx).
