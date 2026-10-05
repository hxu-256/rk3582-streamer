#!/bin/bash
# stream.sh — start mediamtx + ffmpeg-rockchip camera stream
# Usage: ./stream.sh [720p|4k]
#
# 720p → H.264 @ 4Mbps  — works in browser (WebRTC), VLC, ffplay
# 4K   → H.265 @ 25Mbps — use RTSP/HLS only (Chrome/Firefox WebRTC does not support H.265)
#         Connect with: vlc rtsp://<ip>:8554/camera
#         or browser HLS: http://<ip>:8888/camera

MEDIAMTX=${MEDIAMTX:-mediamtx}
MEDIAMTX_CONF=${MTX_CONF:-$(dirname "$0")/mediamtx.yml}
DEVICE=${CAMERA_DEVICE:-/dev/video11}

case "${1:-720p}" in
  720p)
    WIDTH=1280; HEIGHT=720
    CODEC=h264_rkmpp
    BITRATE=4M
    echo "[stream] 720p H.264 @ 4Mbps"
    ;;
  4k)
    WIDTH=3280; HEIGHT=2464
    CODEC=hevc_rkmpp
    BITRATE=40M
    echo "[stream] 4K H.265 @ 40Mbps"
    ;;
  *)
    echo "Usage: $0 [720p|4k]"
    exit 1
    ;;
esac

# Start mediamtx in background
"$MEDIAMTX" "$MEDIAMTX_CONF" &
MTX_PID=$!
echo "[stream] mediamtx started (PID $MTX_PID)"
sleep 2

cleanup() {
  echo ""
  echo "[stream] Stopping..."
  kill $MTX_PID 2>/dev/null
  wait $MTX_PID 2>/dev/null
  exit 0
}
trap cleanup INT TERM

IP=$(ip route get 1 | awk '{print $7; exit}')
echo "[stream] Browser WebRTC : http://${IP}:8889/camera"
echo "[stream] RTSP            : rtsp://${IP}:8554/camera"
echo "[stream] HLS             : http://${IP}:8888/camera"
echo ""

ffmpeg -f v4l2 -use_wallclock_as_timestamps 1 -r 17 \
  -input_format nv12 -video_size "${WIDTH}x${HEIGHT}" \
  -i "$DEVICE" \
  -c:v "$CODEC" -b:v "$BITRATE" -g 30 -bf 0 \
  -f rtsp rtsp://127.0.0.1:8554/camera

kill $MTX_PID 2>/dev/null
wait $MTX_PID 2>/dev/null
