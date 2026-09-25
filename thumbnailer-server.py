#!/usr/bin/env python3
"""
ffmpeg-thumbnailer UNIX domain socket daemon.

GNOME's thumbnail factory runs thumbnailers inside a bubblewrap sandbox with
`--unshare-all` plus a flatpak-derived seccomp filter that denies unshare(),
mount() and clone(CLONE_NEWUSER). No container runtime can start in there --
not podman, not apptainer. So the container runs as a long-lived daemon outside
the sandbox and the in-sandbox shim talks to it over this socket.

Protocol (line oriented, tab separated, UTF-8):
    PING                                 -> OK
    SIZE \t INPUT_PATH \t OUTPUT_PATH    -> OK | ERR: <reason>

Paths are host paths used verbatim: the container runs as the invoking user with
the relevant host trees bind-mounted at their real locations, so no translation
is needed in either direction.
"""

import os
import socket
import socketserver
import subprocess
import sys
import threading

# GNOME's thumbnail sandbox binds only /usr (ro), /proc, /dev, the output directory
# (at /tmp) and the input file. $HOME and $XDG_RUNTIME_DIR are NOT bound, so a socket
# under either is invisible to the shim. The GStreamer plugin cache directory is the
# one writable host path GNOME binds through at its real location -- see add_bwrap()
# in gnome-desktop-thumbnail-script.c -- so the socket lives there.
GST_CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "gnome-desktop-thumbnailer",
    "gstreamer-1.0",
)

SOCK_PATH = os.environ.get(
    "FFT_SOCKET", os.path.join(GST_CACHE_DIR, "ffmpeg-thumbnailer.sock")
)

# Concurrent ffmpeg decodes. GNOME fans out thumbnail requests, and a serialised
# daemon makes the shim time out and GNOME write a permanent fail marker.
MAX_INFLIGHT = int(os.environ.get("FFT_MAX_INFLIGHT", "4"))

# Hardware decode for thumbnails is off by default because it measures slower:
# a thumbnail decodes one frame, and VA-API context plus surface setup and the
# GPU->CPU readback cost more than the decode saves. Measured on Radeon RX 7900 XTX,
# median of 7, software vs VA-API: 1080p H.264 251ms vs 530ms, 4K HEVC 487ms vs 855ms.
# Set FFT_HWACCEL=vaapi|cuda|auto to override. Encode jobs, where hardware wins by a
# wide margin, go through the `encode` and `ffmpeg` apps rather than this daemon.
HWACCEL = os.environ.get("FFT_HWACCEL", "none").strip().lower()
HWACCEL_DEVICE = os.environ.get("FFT_HWACCEL_DEVICE", "").strip()

# Seconds per ffmpeg invocation. The shim waits longer than this.
FFMPEG_TIMEOUT = int(os.environ.get("FFT_FFMPEG_TIMEOUT", "25"))

_slots = threading.Semaphore(MAX_INFLIGHT)


def log(msg):
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def hwaccel_args():
    if HWACCEL in ("", "none", "off", "0"):
        return []
    args = ["-hwaccel", HWACCEL]
    if HWACCEL_DEVICE:
        args += ["-hwaccel_device", HWACCEL_DEVICE]
    return args


def run_ffmpeg(seek, size, in_fd, out_fd):
    # Bounding-box scale that preserves aspect ratio and never upscales.
    vf = (
        f"scale=min({size}\\,iw):min({size}\\,ih)"
        ":force_original_aspect_ratio=decrease"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *hwaccel_args(),
        "-ss", seek,
        "-i", f"/proc/self/fd/{in_fd}",
        "-vf", vf,
        "-vframes", "1",
        "-f", "image2",
        "-c:v", "png",
        "-update", "1",
        f"/proc/self/fd/{out_fd}",
    ]
    try:
        # pass_fds is load-bearing: subprocess closes every inherited descriptor by
        # default, and then /proc/self/fd/N does not exist for the child.
        res = subprocess.run(cmd, timeout=FFMPEG_TIMEOUT, pass_fds=(in_fd, out_fd))
    except subprocess.TimeoutExpired:
        return False
    return res.returncode == 0


def make_thumbnail(size, in_fd, out_fd):
    # The shim runs inside GNOME's sandbox, where the only paths it can name are
    # sandbox-internal (/tmp/<basename>). Those names mean nothing out here, so it
    # hands us open descriptors over SCM_RIGHTS instead and we address them through
    # /proc/self/fd. No path translation, and nothing to break when GNOME changes
    # its bind layout.
    #
    # Fast seek a few seconds in; most videos open on black or a title card.
    # Fall back to the first frame for clips shorter than the seek point.
    with _slots:
        for seek in ("00:00:03", "00:00:00"):
            os.lseek(in_fd, 0, os.SEEK_SET)
            os.ftruncate(out_fd, 0)
            os.lseek(out_fd, 0, os.SEEK_SET)
            if run_ffmpeg(seek, size, in_fd, out_fd) and os.fstat(out_fd).st_size > 0:
                return "OK"
    return "ERR: ffmpeg produced no frame"


def handle_request(data, fds):
    parts = data.decode("utf-8", errors="replace").strip().split("\t")
    verb = parts[0]

    if verb == "PING":
        return "OK"

    if verb != "THUMB" or len(parts) != 2:
        return "ERR: invalid request"
    if len(fds) != 2:
        return "ERR: expected input and output descriptors"

    try:
        size = int(parts[1])
    except ValueError:
        size = 256
    if size <= 0:
        size = 256

    return make_thumbnail(size, fds[0], fds[1])


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        conn = self.request
        conn.settimeout(FFMPEG_TIMEOUT * 2 + 5)
        fds = []
        try:
            data, fds, _flags, _addr = socket.recv_fds(conn, 4096, 2)
            if not data:
                return
            try:
                status = handle_request(data, fds)
            except Exception as exc:  # one bad request must not kill the daemon
                log(f"request failed: {exc!r}")
                status = f"ERR: {exc}"
            try:
                conn.sendall(f"{status}\n".encode("utf-8"))
            except OSError:
                pass  # client gave up
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        log(f"connection error: {sys.exc_info()[1]!r}")


def main():
    rundir = os.path.dirname(SOCK_PATH)
    os.makedirs(rundir, exist_ok=True)
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    server = Server(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o600)

    accel = HWACCEL if HWACCEL not in ("", "none", "off", "0") else "software"
    log(
        f"ffmpeg-thumbnailer listening on {SOCK_PATH} "
        f"(decode={accel}, max_inflight={MAX_INFLIGHT})"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
