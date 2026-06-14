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
- OpenCV: **not installed**
- Flask/FastAPI: **not installed**

## GStreamer environment (installed)
- GStreamer 1.22.9 with plugins-base, plugins-good, plugins-bad, plugins-ugly, libav
- `gst-launch-1.0` and `gst-inspect-1.0` available at `/usr/bin/`
- Rockchip MPP plugin installed (`gstreamer1.0-rockchip1`)

## Web server plan
Stream live camera frames over HTTP to LAN clients using GStreamer + MPP hardware H.264 encoder piped into an HLS or RTSP/TCP stream, served by a small Python (Tornado) HTTP server. Run as a systemd service so MPP initializes at boot before KDE starts.
