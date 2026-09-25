# FFmpeg Video Thumbnailer for GNOME on Immutable Linux

A single Apptainer image that replaces GNOME's video thumbnailer and doubles as a
full ffmpeg for encode work. Built on [jrottenberg/ffmpeg](https://github.com/jrottenberg/ffmpeg).

```
./build.sh vaapi                                    # AMD + Intel   (152 MB)
./build.sh nvidia                                   # + NVIDIA      (514 MB)
apptainer run --app install ffmpeg-thumbnailer-vaapi.sif
```

## Why a daemon

GNOME's thumbnail factory runs every thumbnailer inside a bubblewrap sandbox built by
`add_bwrap()` in `gnome-desktop-thumbnail-script.c`. Two properties of that sandbox
shape this entire design.

**No container runtime can start inside it.** The sandbox combines `--unshare-all`
with a flatpak-derived seccomp filter that denies `unshare`, `setns`, `mount`,
`pivot_root`, `chroot` and `clone` with `CLONE_NEWUSER`. Under a reconstruction of that
filter, apptainer fails exactly where podman does:

```
ERROR : Failed to create user namespace: not allowed to create user namespace
```

So the container runs as a long-lived user service **outside** the sandbox, and a small
shim inside talks to it over a UNIX socket.

**The sandbox binds almost nothing.** Only `/usr` read-only, the usr-merged directories,
`/etc/ld.so.cache`, `/etc/alternatives`, the fontconfig cache, the GStreamer plugin cache,
`/proc`, `/dev`, the output directory (mounted at `/tmp`) and the input file. Two
consequences that are easy to get wrong:

- **`$HOME` and `$XDG_RUNTIME_DIR` are not bound.** A socket in `$XDG_RUNTIME_DIR` is
  invisible from inside. The socket therefore lives in the GStreamer plugin cache
  directory, `~/.cache/gnome-desktop-thumbnailer/gstreamer-1.0/`, which is the one
  writable host path GNOME binds through at its real location.
- **The shim must live under `/usr`.** Nothing in `$HOME` is reachable, so a shim in
  `~/.local/bin` never runs.

## Why file descriptors, not paths

GNOME rewrites the arguments it hands the thumbnailer. `%u` expands to
`file:///tmp/<basename>` and `%o` to `/tmp/<name>.png` — sandbox-internal paths from a
mount namespace the daemon is not in. Forwarding those strings would make the daemon
write thumbnails into its own `/tmp`, and GNOME would find nothing.

So the shim opens both files itself and passes the descriptors over the socket with
`SCM_RIGHTS`. The daemon addresses them as `/proc/self/fd/N`. No path translation, and
nothing to re-break when GNOME changes its bind layout.

```
PING                  -> OK
THUMB \t SIZE  + 2 fds -> OK | ERR: <reason>
```

## Hardware acceleration

Both variants ship the VA drivers upstream omits (`mesa-va-drivers` for AMD,
`intel-media-va-driver` and `i965-va-driver` for Intel); the NVIDIA variant adds
NVENC/NVDEC and needs `--nv` so apptainer injects the host driver libraries. `--app install`
adds that flag automatically when it sees an NVIDIA device.

**Encoding is where this pays.** 20 s of 1080p to HEVC, measured on a Radeon RX 7900 XTX:

| | time |
|---|---|
| `libx265 -preset medium` | 16.6 s |
| `hevc_vaapi` | **2.0 s** |

**Thumbnailing is where it does not.** A thumbnail decodes exactly one frame, and VA-API
context setup plus GPU→CPU readback costs more than the decode saves:

| | software | VA-API |
|---|---|---|
| 1080p H.264 | **251 ms** | 530 ms |
| 4K HEVC | **487 ms** | 855 ms |

The daemon therefore decodes in software by default. Set `FFT_HWACCEL=vaapi` (or `cuda`)
in the unit if your hardware disagrees; `--app probe` shows what is reachable.

## Apps

| command | does |
|---|---|
| *(no app)* | run the daemon in the foreground — what the service starts |
| `--app install` | install shim, thumbnailer entry and user service |
| `--app uninstall` | remove all of it |
| `--app verify` | in-container checks: ffmpeg, GPUs, socket, round trip |
| `--app probe` | report render nodes, VA drivers, hwaccels, hardware encoders |
| `--app thumbnail IN OUT` | one thumbnail, no daemon |
| `--app fix` | regenerate thumbnails GNOME recorded as failed, purge markers |
| `--app encode [-c h264\|hevc\|av1] IN OUT` | NVENC, else VA-API, else software |
| `--app ffmpeg ...` | raw ffmpeg, every codec and hwaccel in the image |

After installing, run the host-side check too — it reproduces GNOME's sandbox exactly:

```
/usr/libexec/ffmpeg-thumbnailer/ffmpeg-thumbnailer-verify
```

## Installing on bootc / ostree

The shim has to land in `/usr`, which is read-only on an image-based system. Either
make it transient:

```
sudo bootc usr-overlay
apptainer run --app install ffmpeg-thumbnailer-vaapi.sif
```

or make it permanent by copying `usr/libexec/ffmpeg-thumbnailer/` into your bootc image.
`--app install` detects a read-only `/usr` and tells you which you are missing. The
`.thumbnailer` entry and the systemd unit always go to `$HOME` and need no privileges.

## Requirements

`squashfuse` **must** be installed. Without it apptainer extracts the whole image into
`$APPTAINER_TMPDIR` on every start rather than mounting it — hundreds of megabytes of
copying, and an outright failure when that path is a small tmpfs. With it, startup is
about 80 ms. `build.sh` and the host verify script both check.

Everything else runs unprivileged: no setuid apptainer, no `--fakeroot`, no root. The
definition file deliberately has no `%post` so that stays true; all package installation
happens in the Containerfile under podman.

## Layout

```
Containerfile.vaapi    AMD + Intel image, on jrottenberg/ffmpeg:9.0-vaapi2404
Containerfile.nvidia   + NVIDIA image,    on jrottenberg/ffmpeg:9.0-nvidia2404
ffmpeg-thumbnailer.def Apptainer definition and the SCIF apps
build.sh               podman build -> OCI archive -> SIF
thumbnailer-server.py  the daemon (runs in the container)
verify.sh              in-container checks (--app verify)
fix-failed-thumbnails.py  fail-cache repair (--app fix)
usr/libexec/ffmpeg-thumbnailer/
    ffmpeg-video-thumbnailer-shim   runs inside GNOME's sandbox
    ffmpeg-thumbnailer-verify       host-side sandbox reproduction
usr/share/thumbnailers/ffmpeg-thumbnailer.thumbnailer
usr/lib/systemd/user/ffmpeg-thumbnailer.service
```

The thumbnailer entry claims the same MIME types as the shipped
`gst-video-thumbnailer.thumbnailer` and is installed under `$XDG_DATA_HOME`, where it
takes precedence over `/usr/share/thumbnailers`.
