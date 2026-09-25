#!/bin/sh
# In-container verification, run via `apptainer run --app verify <sif>`.
# Checks everything observable from inside the bundle. The host-side sandbox test
# lives in ffmpeg-thumbnailer-verify, which `--app install` places next to the shim.
set -u

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[0;33m'; NC='\033[0m'
fail=0
ok()   { printf "${GREEN}  ok${NC}   %s\n" "$1"; }
bad()  { printf "${RED}  FAIL${NC} %s\n" "$1"; fail=1; }
warn() { printf "${YELLOW}  warn${NC} %s\n" "$1"; }
head_() { printf "${BLUE}== %s${NC}\n" "$1"; }

CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
SOCK="${FFT_SOCKET:-$CACHE_HOME/gnome-desktop-thumbnailer/gstreamer-1.0/ffmpeg-thumbnailer.sock}"

head_ "ffmpeg"
if ffmpeg -hide_banner -version >/dev/null 2>&1; then
    ok "$(ffmpeg -hide_banner -version 2>/dev/null | head -1)"
    ok "$(ffmpeg -hide_banner -decoders 2>/dev/null | grep -cE '^ *V') video decoders"
else
    bad "ffmpeg is not runnable"
fi

head_ "hardware"
accels="$(ffmpeg -hide_banner -hwaccels 2>/dev/null | tail -n +2 | tr -d ' ' | tr '\n' ' ')"
[ -n "$accels" ] && ok "hwaccels: $accels" || warn "no hwaccel methods compiled in"
found_gpu=0
for d in /dev/dri/renderD*; do
    [ -e "$d" ] || continue
    drv="$(vainfo --display drm --device "$d" 2>/dev/null | sed -n 's/^vainfo: Driver version: //p' | head -1)"
    if [ -n "$drv" ]; then
        ok "$d -> $drv"
        found_gpu=1
    else
        warn "$d present but no VA-API driver could open it"
    fi
done
if [ -e /dev/nvidiactl ] || [ -e /dev/nvidia0 ]; then
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q nvenc; then
        ok "NVIDIA device present, NVENC encoders available"
        found_gpu=1
    else
        warn "NVIDIA device present but this variant has no NVENC (use the nvidia variant)"
    fi
fi
[ "$found_gpu" = 1 ] || warn "no GPU reachable; encode will fall back to software"

head_ "daemon"
if [ -S "$SOCK" ]; then
    ok "socket $SOCK"
    resp="$(python3 -c '
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(10)
try:
    s.connect(sys.argv[1]); s.sendall(b"PING\n")
    sys.stdout.write(s.recv(64).decode().strip())
except Exception as e:
    sys.stdout.write(f"ERR: {e}")
finally:
    s.close()
' "$SOCK" 2>/dev/null)"
    [ "$resp" = "OK" ] && ok "PING -> OK" || bad "PING -> ${resp:-no response}"
else
    warn "socket not found at $SOCK (daemon not running?)"
fi

head_ "thumbnail round trip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if ffmpeg -y -loglevel error -f lavfi -i testsrc=duration=5:size=1280x720:rate=30 \
          -c:v libx264 -pix_fmt yuv420p "$TMP/sample.mp4" 2>/dev/null; then
    ok "generated sample clip"
else
    bad "could not generate a sample clip"
fi

if [ -S "$SOCK" ] && [ -f "$TMP/sample.mp4" ]; then
    resp="$(python3 -c '
import os, socket, sys
sock_path, size, in_path, out_path = sys.argv[1:5]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(90)
in_fd = out_fd = None
try:
    in_fd = os.open(in_path, os.O_RDONLY)
    out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    s.connect(sock_path)
    socket.send_fds(s, [f"THUMB\t{size}\n".encode()], [in_fd, out_fd])
    sys.stdout.write(s.recv(4096).decode().strip())
except Exception as e:
    sys.stdout.write(f"ERR: {e}")
finally:
    for fd in (in_fd, out_fd):
        if fd is not None:
            os.close(fd)
    s.close()
' "$SOCK" 256 "$TMP/sample.mp4" "$TMP/out.png" 2>/dev/null)"
    if [ "$resp" = "OK" ] && [ -s "$TMP/out.png" ]; then
        ok "daemon produced $(ffprobe -v error -select_streams v:0 \
              -show_entries stream=width,height -of csv=p=0 "$TMP/out.png" 2>/dev/null) PNG"
    else
        bad "daemon thumbnail failed: ${resp:-no response}"
    fi
fi

head_ "fail-cache repair support"
if python3 -c 'import PIL' 2>/dev/null; then
    ok "Pillow available (--app fix will work)"
else
    bad "Pillow missing; --app fix cannot embed Freedesktop PNG metadata"
fi

echo
if [ "$fail" = 0 ]; then
    printf "${GREEN}in-container checks passed${NC}\n"
    printf "Now run the host-side sandbox test:\n"
    printf "  /usr/libexec/ffmpeg-thumbnailer/ffmpeg-thumbnailer-verify\n"
else
    printf "${RED}some checks failed${NC}\n"
fi
exit "$fail"
