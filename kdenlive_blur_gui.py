#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kdenlive Auto-Blur Tool contributors
"""
Kdenlive Auto-Blur Tool
=======================
Detects, tracks and blurs people and vehicles in video. Main use: open an already cut
Kdenlive project, let the tool find the people/vehicles in the parts that are really
used on the timeline, and write a copy of the project with tracking blur effects added.

Run:            python3 kdenlive_blur_gui.py
Dry run:        python3 kdenlive_blur_gui.py project.kdenlive [media_folder] [start] [end]
Repair/install: python3 kdenlive_blur_gui.py --install-deps
"""
__version__ = "1.0.0"
APP_NAME = "Kdenlive Auto-Blur Tool"

import os
import sys
import time
import threading
import json
import platform
import subprocess
import shutil

# ======================================================================================
# Dependency bootstrap - only standard library. If a required component is missing the
# tool offers to download it into a PRIVATE virtual environment (nothing is installed
# system-wide) and restarts itself inside it.
# ======================================================================================
APP_ID = "kdenlive-blur-gui"
MIN_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    sys.exit("%s needs Python %d.%d or newer (found %s)." % ((APP_NAME,) + MIN_PYTHON + (platform.python_version(),)))


def _data_dir():
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "KdenliveBlurApp")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP_ID)


VENV_DIR = os.path.join(_data_dir(), "venv")


def _venv_python(windowed=False):
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "pythonw.exe" if windowed else "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def _in_app_venv():
    return os.path.abspath(sys.prefix) == os.path.abspath(VENV_DIR)


def _distro():
    """'debian' (Debian/Ubuntu/Mint...), 'fedora' (Fedora/RHEL...) or 'other'."""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            data = dict(line.strip().split("=", 1) for line in f if "=" in line)
    except OSError:
        return "other"
    ids = (data.get("ID", "") + " " + data.get("ID_LIKE", "")).replace('"', "").lower()
    if "debian" in ids or "ubuntu" in ids:
        return "debian"
    if "fedora" in ids or "rhel" in ids or "centos" in ids:
        return "fedora"
    return "other"


def system_package_hint(what):
    """The command a user should run to get a missing system component ('tk', 'venv', 'ffmpeg')."""
    d = "windows" if os.name == "nt" else _distro()
    table = {
        ("tk", "debian"): "sudo apt install python3-tk",
        ("tk", "fedora"): "sudo dnf install python3-tkinter",
        ("tk", "windows"): "run the Python installer from python.org again (Modify) and tick 'tcl/tk and IDLE'",
        ("venv", "debian"): "sudo apt install python3-venv",
        ("venv", "fedora"): "sudo dnf install python3",
        ("ffmpeg", "debian"): "sudo apt install ffmpeg",
        ("ffmpeg", "fedora"): "sudo dnf install ffmpeg-free   (or 'ffmpeg' from RPM Fusion)",
        ("ffmpeg", "windows"): "winget install Gyan.FFmpeg   (or download it from https://ffmpeg.org and add it to PATH)",
    }
    return table.get((what, d), "install the '%s' package of your system" % what)


def _fatal(title, message):
    """Show an error even when started from a desktop launcher without a terminal."""
    sys.stderr.write("%s\n%s\n" % (title, message))
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
        except Exception:
            pass
    else:
        for cmd in (["zenity", "--error", "--no-markup", "--title", title, "--text", message],
                    ["kdialog", "--error", message, "--title", title]):
            if shutil.which(cmd[0]):
                try:
                    subprocess.call(cmd)
                    break
                except OSError:
                    pass
    sys.exit(1)


class _InstallError(Exception):
    pass


def _run_logged(cmd, log):
    log("$ " + " ".join(cmd))
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no extra console window on Windows
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env, **kw)
    for line in p.stdout:
        log(line.rstrip())
    return p.wait()


def install_dependencies(variant, log):
    """variant 'cpu': CPU build of PyTorch (small, also used for Intel GPUs through OpenVINO).
    variant 'default': whatever PyPI offers (on Linux a large CUDA build for NVIDIA cards)."""
    os.makedirs(_data_dir(), exist_ok=True)
    if not os.path.exists(_venv_python()):
        log("Creating a private Python environment in " + VENV_DIR)
        if _run_logged([sys.executable, "-m", "venv", VENV_DIR], log) != 0:
            raise _InstallError("Could not create the Python environment.\nTry:  " + system_package_hint("venv"))
    py = _venv_python()
    steps = [[py, "-m", "pip", "install", "--upgrade", "pip"]]
    if variant == "cpu":
        steps.append([py, "-m", "pip", "install", "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"])
    steps.append([py, "-m", "pip", "install", "ultralytics", "opencv-python", "pillow", "openvino"])
    for cmd in steps:
        if _run_logged(cmd, log) != 0:
            raise _InstallError("A download or installation step failed (see the log above).\n"
                                "Check your internet connection, then start the tool again to retry.")
    log("All components installed.")


def _gui_possible():
    return os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


_EXPLAIN = ("The tool needs a few Python components that are not installed yet:\n\n    %s\n\n"
            "It can download and install them now into a private folder\n    %s\n"
            "Nothing is installed system-wide. An internet connection is required.\n"
            "Download size: about 1 GB (CPU / Intel GPU) - much more for the NVIDIA option.")


def _installer_gui(reason):
    """Small Tk window: explanation, choice, progress log. Returns True when everything got installed."""
    import queue
    import tkinter as _tk
    from tkinter import ttk as _ttk
    root = _tk.Tk()
    root.title(APP_NAME + " - install required components")
    root.geometry("680x470")
    result = {"ok": False}
    variant = _tk.StringVar(value="cpu")
    q = queue.Queue()
    frm = _ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)
    _ttk.Label(frm, text=_EXPLAIN % (reason, VENV_DIR), justify="left", wraplength=640).pack(anchor="w")
    choice = _ttk.Frame(frm)
    choice.pack(anchor="w", pady=10)
    _ttk.Radiobutton(choice, text="CPU / Intel GPU through OpenVINO (recommended, smaller download)", variable=variant, value="cpu").pack(anchor="w")
    _ttk.Radiobutton(choice, text="NVIDIA GPU: standard PyTorch from PyPI (very large download on Linux)", variable=variant, value="default").pack(anchor="w")
    log_box = _tk.Text(frm, height=10, state="disabled", wrap="none")
    bar = _ttk.Progressbar(frm, mode="indeterminate")
    buttons = _ttk.Frame(frm)
    buttons.pack(side="bottom", fill="x", pady=(8, 0))

    def append(line):
        log_box.config(state="normal")
        log_box.insert("end", line + "\n")
        log_box.see("end")
        log_box.config(state="disabled")

    def worker(v):
        try:
            install_dependencies(v, lambda line: q.put(("log", line)))
            q.put(("done", ""))
        except _InstallError as e:
            q.put(("error", str(e)))
        except Exception as e:
            q.put(("error", "Unexpected error: %s" % e))

    def poll():
        try:
            while True:
                kind, text = q.get_nowait()
                if kind == "log":
                    append(text)
                elif kind == "done":
                    bar.stop()
                    result["ok"] = True
                    root.after(700, root.destroy)
                    return
                else:
                    bar.stop()
                    append("")
                    append("FAILED: " + text)
                    quit_btn.config(text="Close", state="normal")
                    return
        except queue.Empty:
            pass
        root.after(150, poll)

    def start():
        install_btn.config(state="disabled")
        quit_btn.config(state="disabled")
        choice.pack_forget()
        log_box.pack(fill="both", expand=True, pady=(8, 0))
        bar.pack(fill="x", pady=6)
        bar.start(12)
        threading.Thread(target=worker, args=(variant.get(),), daemon=True).start()
        poll()

    install_btn = _ttk.Button(buttons, text="Download && install", command=start)
    install_btn.pack(side="right", padx=4)
    quit_btn = _ttk.Button(buttons, text="Quit", command=root.destroy)
    quit_btn.pack(side="right")
    root.mainloop()
    return result["ok"]


def _installer_console(reason):
    if not sys.stdin or not sys.stdin.isatty():
        _fatal(APP_NAME, "Required Python components are missing:\n\n    %s\n\n"
                         "Open a terminal and run:\n    python3 %s --install-deps\n"
                         "(or follow the manual steps in the README)." % (reason, os.path.abspath(__file__)))
    print(_EXPLAIN % (reason, VENV_DIR))
    if input("\nInstall now? [Y/n] ").strip().lower() in ("n", "no"):
        return False
    v = input("PyTorch build: [1] CPU / Intel GPU (recommended)  [2] NVIDIA / PyPI default (large)  > ").strip()
    try:
        install_dependencies("default" if v == "2" else "cpu", print)
    except _InstallError as e:
        print("\nFAILED: %s" % e)
        return False
    return True


def _run_installer(reason):
    """Graphical installer when a display exists, otherwise (or if Tk cannot open one) the terminal version."""
    if _gui_possible():
        try:
            return _installer_gui(reason)
        except Exception:
            pass
    return _installer_console(reason)


def _relaunch_in_venv():
    env = dict(os.environ, KBG_BOOTSTRAPPED="1")
    exe = _venv_python(windowed=(os.name == "nt" and sys.executable.lower().endswith("pythonw.exe")))
    args = [exe, os.path.abspath(__file__)] + sys.argv[1:]
    if os.name == "nt":
        sys.exit(subprocess.call(args, env=env))
    os.execve(exe, args, env)


def _bootstrap_after_import_error(err):
    reason = str(err)
    if os.environ.get("KBG_BOOTSTRAPPED") == "1" or _in_app_venv():
        hint = ""
        if "libGL" in reason or "libglib" in reason:
            hint = "\n\nA system library is missing. On Debian/Ubuntu:  sudo apt install libgl1 libglib2.0-0"
        _fatal(APP_NAME, "A component of the private Python environment cannot be loaded:\n\n%s%s\n\n"
                         "To repair it run:\n  python3 kdenlive_blur_gui.py --install-deps" % (reason, hint))
    if os.path.exists(_venv_python()):
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if subprocess.call([_venv_python(), "-c", "import cv2, PIL.ImageTk, ultralytics"], **quiet) == 0:
            _relaunch_in_venv()
    ok = _run_installer(reason)
    if ok:
        _relaunch_in_venv()
    sys.exit(1)


if "--version" in sys.argv:
    print("%s %s" % (APP_NAME, __version__))
    sys.exit(0)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    _fatal(APP_NAME, "Python's Tk module is missing.\n\nInstall it with:\n  " + system_package_hint("tk"))

if "--install-deps" in sys.argv:
    sys.argv.remove("--install-deps")
    _ok = _run_installer("(requested with --install-deps)")
    if _ok:
        _relaunch_in_venv()
    sys.exit(0 if _ok else 1)

try:
    import cv2
    from PIL import Image, ImageTk
    from ultralytics import YOLO
except ImportError as _err:
    _bootstrap_after_import_error(_err)

import xml.etree.ElementTree as ET
import re

import copy
import math
import re
import sys
import types

# ======================================================================================
# Project logic - pure standard library, no GUI/YOLO.
# Reads the cut Kdenlive project, tells the scanner which frames are really used, and
# afterwards inserts the blur filters into the existing timeline.
# ======================================================================================
TRACKER_SERVICE = "opencv.tracker"
BLUR_TYPE_VALUES = {"Median Blur": "0", "Gaussian Blur": "1", "Pixalate": "2", "Opaque Fill": "3"}
GROUPS_PROP = "kdenlive:sequenceproperties.groups"

# Time strings are written 0.25 frame "late". MLT converts clock strings to
# frames with its own rounding, and we do not want a split point to land one
# frame early/late depending on that rounding (would create a 1-frame gap or
# overlap between neighbouring pieces). 0.25 frame is safe for round() and floor().
SUB_FRAME_BIAS = 0.25

_CLOCK_RE = re.compile(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$")


class ProjectError(Exception):
    pass


class Options:
    def __init__(self, blur_strength=20, blur_type="Pixalate", kdenlive_id="opencv.tracker",
                 existing="keep", merge_gap=15, split_audio=True):
        self.blur_strength = int(blur_strength)
        self.blur_type_val = BLUR_TYPE_VALUES.get(blur_type, "2")
        self.kdenlive_id = (kdenlive_id or "").strip() or "opencv.tracker"
        # existing: what to do with clips that already carry an opencv.tracker filter
        #   "keep"    -> leave them completely alone (default, protects manual work)
        #   "replace" -> remove the old tracker filters when new detections exist
        self.existing = existing
        self.merge_gap = int(merge_gap)
        self.split_audio = bool(split_audio)


def _prop(el, name, default=None):
    for p in el.findall("property"):
        if p.get("name") == name:
            return p.text if p.text is not None else ""
    return default


def _set_prop(el, name, value):
    for p in el.findall("property"):
        if p.get("name") == name:
            p.text = str(value)
            return p
    p = ET.Element("property", {"name": name})
    p.text = str(value)
    # keep <property> children before <filter> children
    idx = 0
    for i, ch in enumerate(list(el)):
        if ch.tag == "property":
            idx = i + 1
    el.insert(idx, p)
    return p


def parse_user_time(text, fps):
    """User input -> timeline frame number (None if empty/unreadable).
    Accepts 'SS', 'MM:SS', 'HH:MM:SS' (seconds may have decimals, real time) and
    'HH:MM:SS:FF' (Kdenlive's timecode display, counted at the nominal frame rate)."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        vals = [float(x) for x in t.split(":")]
    except ValueError:
        return None
    if len(vals) == 4:
        h, m, sec, ff = vals
        return int((h * 3600 + m * 60 + sec) * round(fps) + ff)
    if len(vals) > 4:
        return None
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return int(math.floor(sec * fps + 0.5))


def format_clock(seconds):
    """Seconds -> 'HH:MM:SS' (rounded down - also exactly what the time range fields accept)."""
    s = int(max(0, seconds))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def window_range(e, window):
    """Part of clip entry e (as source frames lo..hi) that lies inside the timeline window
    (first_frame, last_frame_or_None). None if the clip is completely outside."""
    if window is None:
        return e.in_f, e.out_f
    ws, we = window
    last = e.pos + e.length - 1
    a = max(ws, e.pos)
    b = last if we is None else min(we, last)
    if a > b:
        return None
    return e.in_f + (a - e.pos), e.in_f + (b - e.pos)


class Track:
    def __init__(self, tid, tractor, is_audio, playlists):
        self.id = tid
        self.tractor = tractor
        self.is_audio = is_audio
        self.playlists = playlists


class Entry:
    def __init__(self, track, playlist, el, pos, in_f, out_f):
        self.track, self.playlist, self.el = track, playlist, el
        self.pos, self.in_f, self.out_f = pos, in_f, out_f
        self.raw_path = None     # resource as written in the project
        self.path = None         # located absolute path (video clips only)
        self.reason = None       # why the entry is not processable

    @property
    def length(self):
        return self.out_f - self.in_f + 1


class KdenliveProject:
    def __init__(self, path):
        self.path = path
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        prof = self.root.find("profile")
        if prof is None:
            raise ProjectError("No <profile> found - not a Kdenlive project?")
        self.fps_num = int(prof.get("frame_rate_num"))
        self.fps_den = int(prof.get("frame_rate_den"))
        self.fps = self.fps_num / self.fps_den
        self.width = int(prof.get("width", 0))
        self.height = int(prof.get("height", 0))
        self.media_root = self.root.get("root") or ""
        self.parse_mode = "floor"       # MLT truncates clock strings to whole frames (verified against the group positions in real Kdenlive projects); the self-test also tries "round"
        self.by_id = {}
        for el in self.root:
            if el.get("id"):
                self.by_id[el.get("id")] = el
        self.warnings = []
        self._find_sequence()

    # ------------------------------------------------------------------ time
    def parse_time(self, s):
        s = (s or "").strip()
        if not s:
            return 0
        m = _CLOCK_RE.match(s)
        frames = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) * self.fps if m else float(s)
        return int(math.floor(frames + 0.5)) if self.parse_mode == "round" else int(math.floor(frames))

    def frame_to_tc(self, f):
        ms = int(round((f + SUB_FRAME_BIAS) * 1000.0 * self.fps_den / self.fps_num))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, msec = divmod(rem, 1000)
        return "%02d:%02d:%02d.%03d" % (h, m, s, msec)

    # ------------------------------------------------------------- structure
    def _find_sequence(self):
        main_bin = self.by_id.get("main_bin")
        active = _prop(main_bin, "kdenlive:docproperties.activetimeline") if main_bin is not None else None
        seq = None
        for t in self.root.findall("tractor"):
            if active and _prop(t, "kdenlive:uuid") == active:
                seq = t
                break
        if seq is None:   # fall back to the project tractor's first track
            for t in self.root.findall("tractor"):
                if _prop(t, "kdenlive:projectTractor") == "1":
                    tr = t.find("track")
                    if tr is not None:
                        seq = self.by_id.get(tr.get("producer"))
        if seq is None:
            raise ProjectError("Could not find the active sequence in the project.")
        self.sequence = seq
        self.sequence_name = _prop(seq, "kdenlive:clipname", seq.get("id"))
        self.tracks = []
        refs = [t.get("producer") for t in seq.findall("track")]
        # index 0 is the black background track; Kdenlive track ids start after it
        for idx, ref in enumerate(refs):
            el = self.by_id.get(ref)
            if idx == 0 or el is None or el.tag != "tractor":
                continue
            pls = [self.by_id[t.get("producer")] for t in el.findall("track")
                   if self.by_id.get(t.get("producer")) is not None and self.by_id[t.get("producer")].tag == "playlist"]
            self.tracks.append(Track(idx - 1, el, _prop(el, "kdenlive:audio_track") == "1", pls))

    def _media_info(self, producer_el):
        """-> (raw_path, reason). raw_path is None if the clip cannot be anonymized."""
        if producer_el is None or producer_el.tag not in ("chain", "producer"):
            return None, "not a plain clip (nested sequence?)"
        service = _prop(producer_el, "mlt_service", "")
        if not service.startswith("avformat"):
            return None, "service '%s' is not a video file" % service
        nb = int(_prop(producer_el, "meta.media.nb_streams", "0") or 0)
        types = [_prop(producer_el, "meta.media.%d.stream.type" % i) for i in range(nb)]
        if nb and "video" not in types:
            return None, "no video stream"
        res = _prop(producer_el, "kdenlive:originalurl") or _prop(producer_el, "resource")
        if not res:
            return None, "no resource"
        w, h = _prop(producer_el, "meta.media.width"), _prop(producer_el, "meta.media.height")
        if w and h and (int(w), int(h)) != (self.width, self.height):
            self.warnings.append("%s is %sx%s but the project is %dx%d - tracker coordinates may be off."
                                 % (os.path.basename(res), w, h, self.width, self.height))
        return res, None

    def list_entries(self, video_only=False):
        out = []
        for tr in self.tracks:
            if video_only and tr.is_audio:
                continue
            for pl in tr.playlists:
                pos = 0
                for ch in list(pl):
                    if ch.tag == "blank":
                        pos += self.parse_time(ch.get("length"))
                    elif ch.tag == "entry":
                        in_f, out_f = self.parse_time(ch.get("in")), self.parse_time(ch.get("out"))
                        e = Entry(tr, pl, ch, pos, in_f, out_f)
                        raw, why = self._media_info(self.by_id.get(ch.get("producer")))
                        e.raw_path, e.reason = raw, why
                        out.append(e)
                        pos += out_f - in_f + 1
        return out

    def locate(self, raw_path, media_dir=None):
        cands = []
        if os.path.isabs(raw_path):
            cands.append(raw_path)
        else:
            cands.append(os.path.join(self.media_root, raw_path))
        if media_dir:
            cands.append(os.path.join(media_dir, os.path.basename(raw_path)))
        for c in cands:
            if os.path.exists(c):
                return c
        return cands[-1]    # best guess, caller reports it as missing

    @staticmethod
    def _tracker_filters(entry_el):
        return [f for f in entry_el.findall("filter") if _prop(f, "mlt_service") == TRACKER_SERVICE]

    # ---------------------------------------------------------------- planning
    def plan_jobs(self, media_dir=None, pad=2, existing="keep", window=None):
        """Source frame ranges the detector has to process: {abs_path: [(first, last), ...]}.
        Only frames that are really used on the timeline (and inside the optional timeline
        window), merged and padded."""
        jobs, skipped, kept = {}, [], 0
        self.tl_map = {}      # path -> [(in_f, out_f, timeline_pos)] of the clips considered, for time display
        for e in self.list_entries(video_only=True):
            if e.raw_path is None:
                skipped.append((e, e.reason))
                continue
            rng = window_range(e, window)
            if rng is None:
                continue
            if existing == "keep" and self._tracker_filters(e.el):
                kept += 1
                continue
            e.path = self.locate(e.raw_path, media_dir)
            self.tl_map.setdefault(e.path, []).append((e.in_f, e.out_f, e.pos))
            jobs.setdefault(e.path, []).append((max(0, rng[0] - pad), rng[1] + pad))
        for p, rng in jobs.items():
            rng.sort()
            merged = [list(rng[0])]
            for a, b in rng[1:]:
                if a <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            jobs[p] = [tuple(x) for x in merged]
        return jobs, skipped, kept

    def plan_schedule(self, media_dir=None, pad=2, existing="keep", window=None):
        """Like plan_jobs, but as a list [(path, first, last, timeline_start_frame)] sorted by
        timeline position - so processing runs from the beginning of the timeline to its end
        and a stopped run can be continued from a timestamp."""
        jobs, skipped, kept = self.plan_jobs(media_dir, pad, existing, window)
        items = []
        for path, ranges in jobs.items():
            for a, b in ranges:
                tls = [pos + (max(a, in_f) - in_f) for in_f, out_f, pos in self.tl_map.get(path, [])
                       if in_f <= b and out_f >= a]
                items.append((min(tls) if tls else 0, path, a, b))
        items.sort()
        return [(path, a, b, tl) for tl, path, a, b in items], skipped, kept

    def timeline_frame(self, path, source_frame):
        """Source frame of a file -> position on the timeline (None if not on it)."""
        for in_f, out_f, pos in getattr(self, "tl_map", {}).get(path, []):
            if in_f <= source_frame <= out_f:
                return pos + (source_frame - in_f)
        return None

    def timeline_end_frame(self):
        """Last frame used by any clip on the timeline."""
        return max([e.pos + e.length - 1 for e in self.list_entries()] or [0])

    # ----------------------------------------------------------------- editing
    def _max_filter_id(self):
        n = 0
        for el in self.root.iter("filter"):
            m = re.match(r"^filter(\d+)$", el.get("id") or "")
            if m:
                n = max(n, int(m.group(1)))
        return n

    def _new_filter(self, coords, b_start, b_end, opts):
        """One opencv.tracker filter for one tracked object inside [b_start, b_end]."""
        self._fid += 1
        tc = self.frame_to_tc
        coords = sorted(coords, key=lambda c: c[0])
        first_f, last_f = coords[0][0], coords[-1][0]
        parts = []
        if first_f > b_start:
            parts.append("%s&=0 0 0 0" % tc(b_start))
            if first_f - 1 > b_start:
                parts.append("%s&=0 0 0 0" % tc(first_f - 1))
        for f, x, y, w, h in coords:
            parts.append("%s$=%d %d %d %d" % (tc(f), x, y, w, h))
        if last_f < b_end:
            parts.append("%s&=0 0 0 0" % tc(last_f + 1))
        flt = ET.Element("filter", {"id": "filter%d" % self._fid})
        for name, val in (("shape_width", "0"), ("steps", "5"), ("algo", "MEDIANFLOW"),
                          ("mlt_service", TRACKER_SERVICE), ("kdenlive_id", opts.kdenlive_id),
                          ("results", ";".join(parts)), ("shape", "0"), ("shape_color", "0xff0000ff"),
                          ("blur", str(opts.blur_strength)), ("blur_type", opts.blur_type_val),
                          ("modelsfolder", ""), ("rect", "50"), ("kdenlive:collapsed", "0")):
            ET.SubElement(flt, "property", {"name": name}).text = val
        return flt

    def _clone_piece(self, entry_el, s, t, is_first, is_last):
        el = copy.deepcopy(entry_el)
        el.set("in", entry_el.get("in") if is_first else self.frame_to_tc(s))
        el.set("out", entry_el.get("out") if is_last else self.frame_to_tc(t))
        return el

    @staticmethod
    def _blocks(frames, gap):
        blocks = []
        for f in frames:
            if blocks and f - blocks[-1][1] <= gap:
                blocks[-1][1] = f
            else:
                blocks.append([f, f])
        return [tuple(b) for b in blocks]

    def _replace(self, playlist, old_el, new_els):
        idx = list(playlist).index(old_el)
        playlist.remove(old_el)
        for i, el in enumerate(new_els):
            playlist.insert(idx + i, el)

    def apply_tracks(self, tracks_by_path, opts, media_dir=None, window=None):
        """tracks_by_path: {abs_path: {track_key: [(frame, x, y, w, h), ...]}}  (source frames).
        window: optional (first_frame, last_frame_or_None) on the timeline - only that part is edited.
        Returns a stats dict. The project is modified in memory; call save()."""
        self._fid = self._max_filter_id()
        stats = dict(clips=0, processed=0, kept_existing=0, no_detections=0, not_video=0, outside_range=0,
                     pieces=0, filters=0, whole_clip_mode=0, audio_split=0, groups_rewritten=0)
        all_entries = self.list_entries()
        index = {(e.track.id, e.pos): e for e in all_entries}
        splits = []
        for e in all_entries:
            if e.track.is_audio:
                continue
            stats["clips"] += 1
            if e.raw_path is None:
                stats["not_video"] += 1
                continue
            rng = window_range(e, window)
            if rng is None:
                stats["outside_range"] += 1
                continue
            lo, hi = rng
            existing = self._tracker_filters(e.el)
            if existing and opts.existing == "keep":
                stats["kept_existing"] += 1
                continue
            e.path = self.locate(e.raw_path, media_dir)
            trk = tracks_by_path.get(e.path) or tracks_by_path.get(e.raw_path) or {}
            mine = {}
            for key, coords in trk.items():
                sub = [c for c in coords if lo <= c[0] <= hi]
                if sub:
                    mine[key] = sub
            if not mine:
                stats["no_detections"] += 1
                continue
            for f in existing:
                e.el.remove(f)
            frames = sorted({c[0] for coords in mine.values() for c in coords})
            blocks = self._blocks(frames, opts.merge_gap)
            foreign = e.el.findall("filter")
            if foreign:
                # other effects on the clip: do not cut it, just add the trackers on top
                stats["whole_clip_mode"] += 1
                for coords in mine.values():
                    e.el.append(self._new_filter(coords, e.in_f, e.out_f, opts))
                    stats["filters"] += 1
                _set_prop(e.el, "kdenlive:activeeffect", "0")
                stats["processed"] += 1
                continue
            pieces, cur = [], e.in_f
            for a, b in blocks:
                if a > cur:
                    pieces.append((cur, a - 1, False))
                pieces.append((a, b, True))
                cur = b + 1
            if cur <= e.out_f:
                pieces.append((cur, e.out_f, False))
            new_els = []
            for i, (s, t, has) in enumerate(pieces):
                el = self._clone_piece(e.el, s, t, i == 0, i == len(pieces) - 1)
                if has:
                    _set_prop(el, "kdenlive:activeeffect", "0")
                    for coords in mine.values():
                        sub = [c for c in coords if s <= c[0] <= t]
                        if sub:
                            el.append(self._new_filter(sub, s, t, opts))
                            stats["filters"] += 1
                new_els.append(el)
            self._replace(e.playlist, e.el, new_els)
            stats["pieces"] += len(pieces)
            stats["processed"] += 1
            splits.append((e, pieces))
        if opts.split_audio and splits:
            self._split_audio_and_groups(splits, index, stats)
        return stats

    def _split_audio_and_groups(self, splits, index, stats):
        prop = None
        for p in self.sequence.findall("property"):
            if p.get("name") == GROUPS_PROP:
                prop = p
        if prop is None or not (prop.text or "").strip():
            return
        try:
            groups = json.loads(prop.text)
        except ValueError:
            self.warnings.append("Group data could not be parsed - A/V groups left unchanged.")
            return

        def leaf(ch):
            if ch.get("type") != "Leaf":
                return None
            parts = (ch.get("data") or "").split(":")
            return (int(parts[0]), int(parts[1])) if len(parts) >= 2 else None

        leaf_index = {}
        for gi, g in enumerate(groups):
            if g.get("type") == "AVSplit":
                for ch in g.get("children", []):
                    k = leaf(ch)
                    if k:
                        leaf_index[k] = gi
        def find_entry(tid, gpos):
            """Group positions can be off by one frame next to blanks (clock rounding of
            blank lengths), so look at the neighbouring frames too. -> (entry, offset)"""
            for d in (0, 1, -1):
                e = index.get((tid, gpos + d))
                if e is not None:
                    return e, d
            return None, 0

        replaced = {}
        for ve, pieces in splits:
            gi, vd = None, 0
            for d in (0, -1, 1):          # group coordinate = our position + d
                if (ve.track.id, ve.pos + d) in leaf_index:
                    gi, vd = leaf_index[(ve.track.id, ve.pos + d)], d
                    break
            if gi is None or gi in replaced:
                continue
            children = groups[gi].get("children", [])
            keys = [leaf(c) for c in children]
            if len(children) != 2 or None in keys:
                continue
            vkey = (ve.track.id, ve.pos + vd)
            other = [k for k in keys if k != vkey]
            ae, ad = find_entry(*other[0]) if other else (None, 0)
            ok = (ae is not None and ae.track.is_audio and ae.raw_path == ve.raw_path
                  and (ae.in_f, ae.out_f) == (ve.in_f, ve.out_f)
                  and not any(f.get("in") or f.get("out") for f in ae.el.findall("filter")))
            if not ok:
                self.warnings.append("Clip at frame %d: grouped audio does not match - group left as is." % ve.pos)
                continue
            audio_els = [self._clone_piece(ae.el, s, t, i == 0, i == len(pieces) - 1)
                         for i, (s, t, _) in enumerate(pieces)]
            self._replace(ae.playlist, ae.el, audio_els)
            new_groups = []
            for s, t, _ in pieces:
                new_groups.append({"children": [{"data": "%d:%d:-1" % (k[0], k[1] + (s - ve.in_f)),
                                                 "leaf": "clip", "type": "Leaf"} for k in keys],
                                   "type": "AVSplit"})
            replaced[gi] = new_groups
            stats["audio_split"] += 1
        if replaced:
            out = []
            for gi, g in enumerate(groups):
                out.extend(replaced[gi] if gi in replaced else [g])
            stats["groups_rewritten"] = len(replaced)
            prop.text = json.dumps(out, indent=4)

    def save(self, out_path):
        if os.path.abspath(out_path) == os.path.abspath(self.path):
            raise ProjectError("Refusing to overwrite the input project - choose a different output file.")
        ET.indent(self.root, space=" ")
        self.tree.write(out_path, encoding="utf-8", xml_declaration=True)


def extend_tails(track_history, tail_frames):
    """Repeat each track's last box for tail_frames frames (same behaviour as the original tool)."""
    if tail_frames <= 0:
        return
    for coords in track_history.values():
        if not coords:
            continue
        coords.sort(key=lambda c: c[0])
        lf, lx, ly, lw, lh = coords[-1]
        for k in range(1, tail_frames + 1):
            coords.append((lf + k, lx, ly, lw, lh))


def plan_cli(argv):
    if len(argv) < 2:
        print("usage: python kdenlive_blur_gui.py project.kdenlive [media_dir] [start] [end]\n"
              "Prints which source frame ranges would be analysed (dry run).")
        return 1
    proj = KdenliveProject(argv[1])
    media_dir = argv[2] if len(argv) > 2 else None
    print("Sequence: %s | profile %dx%d @ %.3f fps" % (proj.sequence_name, proj.width, proj.height, proj.fps))
    for t in proj.tracks:
        n = sum(len([c for c in pl if c.tag == 'entry']) for pl in t.playlists)
        print("  track id %d: %s, %d clips" % (t.id, "audio" if t.is_audio else "video", n))
    window = None
    if len(argv) > 3:
        ws = parse_user_time(argv[3], proj.fps) or 0
        we = parse_user_time(argv[4], proj.fps) if len(argv) > 4 else None
        window = (ws, we)
        print("Timeline window: frames %d - %s" % (ws, we if we is not None else "end"))
    jobs, skipped, kept = proj.plan_jobs(media_dir, window=window)
    total = 0
    for p, rng in jobs.items():
        fr = sum(b - a + 1 for a, b in rng)
        total += fr
        print("  %s%s: %d range(s), %d frames" % (p, "" if os.path.exists(p) else "  [NOT FOUND]", len(rng), fr))
    print("Total frames to analyse: %d (%.1f min of video)" % (total, total / proj.fps / 60))
    print("Clips that already have a tracker effect (kept): %d" % kept)
    for e, why in skipped:
        print("  skipped clip at frame %d: %s" % (e.pos, why))
    for w in proj.warnings:
        print("  warning:", w)
    return 0


ka = types.SimpleNamespace(KdenliveProject=KdenliveProject, Options=Options, extend_tails=extend_tails,
                           parse_user_time=parse_user_time, format_clock=format_clock)
# ======================================================================================

class BlurApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1025x1340")
        self.root.resizable(False, False)

        # --- Configuration Variables ---
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        
        # Optional Time Range Filtering
        self.start_time_var = tk.StringVar(value="")
        self.end_time_var = tk.StringVar(value="")
        
        # Advanced Parameters (with defaults)
        self.mode_var = tk.StringVar(value="both")
        self.export_mode_var = tk.StringVar(value="kdenlive") # "kdenlive" = edit an existing project, "mkv" = check video from a video file
        self.model_var = tk.StringVar(value="yolo26s.pt")
        self.device_var = tk.StringVar(value="intel:gpu") # "intel:gpu", "cuda:0", "cpu"
        self.conf_var = tk.DoubleVar(value=0.45)
        self.imgsz_var = tk.IntVar(value=640)
        self.blur_strength_var = tk.IntVar(value=20)
        self.blur_type_var = tk.StringVar(value="Pixalate") # Pixalate=2, Gaussian=1, Median=0, Opaque=3
        self.kdenlive_id_var = tk.StringVar(value="opencv.tracker") # Standard default filter ID
        self.tail_buffer_var = tk.IntVar(value=3)
        self.media_dir_var = tk.StringVar(value="")        # optional: where the project's source videos live
        self.existing_var = tk.StringVar(value="keep")     # timeline mode: clips that already have a tracker effect
        self.preview_sec_var = tk.StringVar(value="1.0")   # preview every N seconds of video, decimals allowed, 0 = off
        self.running = False
        self.cancel_event = threading.Event()
        
        # Size Filters
        self.person_min_h_var = tk.IntVar(value=25)
        self.person_min_area_var = tk.IntVar(value=900)
        self.veh_min_h_var = tk.IntVar(value=50)
        self.veh_min_area_var = tk.IntVar(value=3500)

        # Thread-safe UI String Variables
        self.status_var = tk.StringVar(value="Status: Ready")
        self.counts_var = tk.StringVar(value="Detections (Unique Tracks): 0")
        self.eta_var = tk.StringVar(value="Estimated Time Remaining: --:--")
        
        self.photo_ref = None
        self.preview_image_id = None
        self.preview_text_id = None

        # Load saved settings on startup
        self.load_settings_from_disk(CONFIG_FILE_PATH, silent=True)

        self.create_widgets()

    def create_widgets(self):
        title_label = tk.Label(self.root, text="Landscape Privacy Anonymizer & Verifier", font=("Arial", 16, "bold"))
        title_label.pack(pady=5)

        # --- Main Notebook (Tabs) ---
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=15, pady=5)

        tab_main = ttk.Frame(notebook)
        tab_config = ttk.Frame(notebook)
        
        notebook.add(tab_main, text=" Processing & Execution ")
        notebook.add(tab_config, text=" Advanced Configuration ")

        # ================= TAB 1: MAIN PROCESSING =================
        file_frame = tk.LabelFrame(tab_main, text=" File Configuration ", font=("Arial", 10, "bold"), padx=10, pady=5)
        file_frame.pack(fill="x", padx=10, pady=5)

        self.input_label = tk.Label(file_frame, text="Kdenlive Project:")
        self.input_label.grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(file_frame, textvariable=self.input_path, width=62).grid(row=0, column=1, padx=5, pady=2)
        tk.Button(file_frame, text="Browse...", command=self.browse_input).grid(row=0, column=2, pady=2)

        tk.Label(file_frame, text="Output Target:").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(file_frame, textvariable=self.output_path, width=62).grid(row=1, column=1, padx=5, pady=2)
        tk.Button(file_frame, text="Save As...", command=self.browse_output).grid(row=1, column=2, pady=2)

        media_label = tk.Label(file_frame, text="Media Folder (optional):")
        media_label.grid(row=2, column=0, sticky="w", pady=2)
        media_entry = tk.Entry(file_frame, textvariable=self.media_dir_var, width=62)
        media_entry.grid(row=2, column=1, padx=5, pady=2)
        media_button = tk.Button(file_frame, text="Browse...", command=self.browse_media_dir)
        media_button.grid(row=2, column=2, pady=2)
        self.media_widgets = [media_label, media_entry, media_button]   # only used in project mode

        # Time Range & Quick Options
        time_frame = tk.LabelFrame(tab_main, text=" Time Range (optional) - Kdenlive: timeline time as HH:MM:SS, MKV: video time ", font=("Arial", 10, "bold"), padx=10, pady=5)
        time_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(time_frame, text="Start Time (empty = from the beginning):").grid(row=0, column=0, sticky="w", pady=2)
        start_entry = tk.Entry(time_frame, textvariable=self.start_time_var, width=18)
        start_entry.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        tk.Label(time_frame, text="End Time (empty = until the end):").grid(row=0, column=2, sticky="w", pady=2, padx=(15, 0))
        end_entry = tk.Entry(time_frame, textvariable=self.end_time_var, width=18)
        end_entry.grid(row=0, column=3, sticky="w", padx=5, pady=2)
        self.time_entries = [start_entry, end_entry]

        quick_frame = tk.Frame(tab_main, pady=5)
        quick_frame.pack(fill="x", padx=10)

        tk.Label(quick_frame, text="Mode (kdenlive = edit project):").grid(row=0, column=0, sticky="w", pady=2)
        self.export_combo = ttk.Combobox(quick_frame, textvariable=self.export_mode_var, values=["kdenlive", "mkv"], state="readonly", width=10)
        self.export_combo.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        self.export_combo.bind("<<ComboboxSelected>>", self.on_export_mode_changed)

        tk.Label(quick_frame, text="Target Mode:").grid(row=0, column=2, sticky="w", pady=2, padx=(20, 0))
        ttk.Combobox(quick_frame, textvariable=self.mode_var, values=["both", "vehicles", "persons"], state="readonly", width=15).grid(row=0, column=3, sticky="w", padx=5, pady=2)

        tk.Label(quick_frame, text="Preview Interval (seconds, 0 = off):").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(quick_frame, textvariable=self.preview_sec_var, width=8).grid(row=1, column=1, sticky="w", padx=5, pady=2)
        tk.Label(quick_frame, text="decimals allowed, e.g. 0.5 = twice per video second", fg="gray").grid(row=1, column=2, columnspan=2, sticky="w", padx=(20, 0))

        self.run_btn = tk.Button(tab_main, text="Start Processing & Generation", bg="#4CAF50", fg="white", font=("Arial", 11, "bold"), command=self.on_run_button)
        self.run_btn.pack(fill="x", padx=10, pady=8)

        # Live Preview (16:9 Canvas)
        preview_frame = tk.LabelFrame(tab_main, text=" Live Processing Preview (16:9 Aspect Ratio, interval adjustable above) ", font=("Arial", 10, "bold"), padx=10, pady=5)
        preview_frame.pack(fill="x", padx=10, pady=5)

        self.preview_canvas = tk.Canvas(preview_frame, width=960, height=540, bg="black")
        self.preview_canvas.pack(pady=3)
        self.preview_text_id = self.preview_canvas.create_text(
            480, 270, text="Preview will appear here when processing starts", fill="white", font=("Arial", 12)
        )

        # Stats & ETA
        stats_frame = tk.LabelFrame(tab_main, text=" Progress & Statistics ", font=("Arial", 10, "bold"), padx=10, pady=5)
        stats_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(stats_frame, textvariable=self.counts_var, font=("Arial", 9, "bold")).pack(anchor="w", pady=1)
        tk.Label(stats_frame, textvariable=self.eta_var, font=("Arial", 9, "bold", "italic"), fg="#1976D2").pack(anchor="w", pady=1)

        tk.Label(self.root, textvariable=self.status_var, font=("Arial", 9, "italic"), fg="gray").pack(pady=2)


        # ================= TAB 2: ADVANCED CONFIGURATION =================
        cfg_inner = tk.Frame(tab_config, padx=15, pady=15)
        cfg_inner.pack(fill="both", expand=True)

        inf_frame = tk.LabelFrame(cfg_inner, text=" YOLO Inference & Hardware Parameters ", font=("Arial", 10, "bold"), padx=10, pady=10)
        inf_frame.pack(fill="x", pady=5)

        tk.Label(inf_frame, text="YOLO Model Version:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(inf_frame, textvariable=self.model_var, values=["yolo26s.pt", "yolo26n.pt", "yolo11s.pt", "yolo11n.pt"], state="readonly", width=18).grid(row=0, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Execution Device (Hardware):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(inf_frame, textvariable=self.device_var, values=["intel:gpu", "intel:cpu", "intel:npu", "cuda:0", "cpu"], state="readonly", width=18).grid(row=1, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Confidence Threshold (0.1 - 0.9):").grid(row=2, column=0, sticky="w", pady=4)
        tk.Spinbox(inf_frame, from_=0.1, to=0.9, increment=0.05, textvariable=self.conf_var, width=15).grid(row=2, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="YOLO Input Image Size (imgsz):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(inf_frame, textvariable=self.imgsz_var, values=[640, 960, 1280], state="readonly", width=15).grid(row=3, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Blur Intensity (Kdenlive / Direct):").grid(row=4, column=0, sticky="w", pady=4)
        tk.Spinbox(inf_frame, from_=5, to=50, increment=5, textvariable=self.blur_strength_var, width=15).grid(row=4, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Kdenlive Blur Type:").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(inf_frame, textvariable=self.blur_type_var, values=["Pixalate", "Gaussian Blur", "Median Blur", "Opaque Fill"], state="readonly", width=18).grid(row=5, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Kdenlive Filter Name (kdenlive_id):").grid(row=6, column=0, sticky="w", pady=4)
        tk.Entry(inf_frame, textvariable=self.kdenlive_id_var, width=20).grid(row=6, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Tail Buffer Extension (Frames):").grid(row=7, column=0, sticky="w", pady=4)
        tk.Spinbox(inf_frame, from_=0, to=10, increment=1, textvariable=self.tail_buffer_var, width=15).grid(row=7, column=1, sticky="w", padx=10, pady=4)

        tk.Label(inf_frame, text="Existing Tracker Effects (timeline mode):").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Combobox(inf_frame, textvariable=self.existing_var, values=["keep", "replace"], state="readonly", width=18).grid(row=8, column=1, sticky="w", padx=10, pady=4)
        tk.Label(inf_frame, text="keep = clips that already have a tracker effect are left untouched, replace = redo them", fg="gray").grid(row=8, column=2, sticky="w")

        size_frame = tk.LabelFrame(cfg_inner, text=" Size Filtering Thresholds ", font=("Arial", 10, "bold"), padx=10, pady=10)
        size_frame.pack(fill="x", pady=10)

        tk.Label(size_frame, text="Persons / Bikes / Mopeds Min Height (px):").grid(row=0, column=0, sticky="w", pady=4)
        tk.Spinbox(size_frame, from_=10, to=100, increment=5, textvariable=self.person_min_h_var, width=15).grid(row=0, column=1, sticky="w", padx=10, pady=4)

        tk.Label(size_frame, text="Persons / Bikes / Mopeds Min Area (px²):").grid(row=1, column=0, sticky="w", pady=4)
        tk.Spinbox(size_frame, from_=200, to=5000, increment=100, textvariable=self.person_min_area_var, width=15).grid(row=1, column=1, sticky="w", padx=10, pady=4)

        tk.Label(size_frame, text="Cars / Trucks / Buses Min Height (px):").grid(row=2, column=0, sticky="w", pady=4)
        tk.Spinbox(size_frame, from_=20, to=150, increment=5, textvariable=self.veh_min_h_var, width=15).grid(row=2, column=1, sticky="w", padx=10, pady=4)

        tk.Label(size_frame, text="Cars / Trucks / Buses Min Area (px²):").grid(row=3, column=0, sticky="w", pady=4)
        tk.Spinbox(size_frame, from_=1000, to=15000, increment=500, textvariable=self.veh_min_area_var, width=15).grid(row=3, column=1, sticky="w", padx=10, pady=4)

        pers_frame = tk.Frame(cfg_inner, pady=10)
        pers_frame.pack(fill="x")

        tk.Button(pers_frame, text="Save Settings as Default", bg="#2196F3", fg="white", font=("Arial", 9, "bold"), command=self.save_current_settings).pack(side="left", padx=5)
        tk.Button(pers_frame, text="Export Settings to File...", command=self.export_settings_file).pack(side="left", padx=5)
        tk.Button(pers_frame, text="Import Settings from File...", command=self.import_settings_file).pack(side="left", padx=5)

        self.apply_mode_ui()

    def get_initial_dir(self, current_path):
        """Helper to resolve the directory of the current path for file dialogs."""
        if current_path and os.path.exists(current_path):
            d = os.path.dirname(current_path)
            if os.path.exists(d):
                return d
        inp = self.input_path.get()
        if inp:
            d = os.path.dirname(inp)
            if os.path.exists(d):
                return d
        return ""

    def apply_mode_ui(self):
        """Show only the fields that make sense for the selected mode."""
        project = self.export_mode_var.get() != "mkv"
        self.input_label.config(text="Kdenlive Project:" if project else "Input Video:")
        for w in self.media_widgets:
            if project:
                w.grid()
            else:
                w.grid_remove()

    def suggest_output(self):
        """Proposes <input name>_edited.kdenlive (or .mkv) next to the input file."""
        inp = self.input_path.get()
        if not inp:
            return
        base, _ = os.path.splitext(inp)
        ext = ".mkv" if self.export_mode_var.get() == "mkv" else ".kdenlive"
        m = re.search(r"_edited(\d*)$", base)      # continuing from an earlier result: _edited -> _edited2 -> _edited3
        if m:
            base = base[:m.start()] + "_edited" + str(int(m.group(1) or 1) + 1)
        else:
            base += "_edited"
        self.output_path.set(f"{base}{ext}")

    def on_export_mode_changed(self, event=None):
        project = self.export_mode_var.get() != "mkv"
        inp = self.input_path.get()
        if inp and inp.lower().endswith(".kdenlive") != project:
            self.input_path.set("")          # the chosen file does not fit the new mode
            self.output_path.set("")
        else:
            self.suggest_output()
        self.apply_mode_ui()

    def browse_input(self):
        initial = self.get_initial_dir(self.input_path.get())
        project = self.export_mode_var.get() != "mkv"
        if project:
            title, types = "Select Kdenlive Project", [("Kdenlive Project", "*.kdenlive"), ("All Files", "*.*")]
        else:
            title, types = "Select Video File", [("Video Files", "*.mp4 *.mov *.mkv *.avi"), ("All Files", "*.*")]
        filename = filedialog.askopenfilename(title=title, initialdir=initial, filetypes=types)
        if filename:
            if filename.lower().endswith(".kdenlive") and not project:
                self.export_mode_var.set("kdenlive")
                self.apply_mode_ui()
            self.input_path.set(filename)
            self.suggest_output()
            self.save_current_settings(silent=True)

    def browse_media_dir(self):
        d = filedialog.askdirectory(title="Folder containing the project's source videos")
        if d:
            self.media_dir_var.set(d)
            self.save_current_settings(silent=True)

    def browse_output(self):
        initial = self.get_initial_dir(self.output_path.get())
        ext = ".mkv" if self.export_mode_var.get() == "mkv" else ".kdenlive"
        filetypes = [("Verification Video (.mkv)", "*.mkv")] if ext == ".mkv" else [("Kdenlive Project (.kdenlive)", "*.kdenlive")]
        filename = filedialog.asksaveasfilename(
            title="Save Output File As", 
            initialdir=initial, 
            defaultextension=ext, 
            filetypes=filetypes
        )
        if filename:
            self.output_path.set(filename)
            self.save_current_settings(silent=True)

    def get_settings_dict(self):
        return {
            "mode": self.mode_var.get(),
            "export_mode": self.export_mode_var.get(),
            "model_name": self.model_var.get(),
            "device": self.device_var.get(),
            "conf": self.conf_var.get(),
            "imgsz": self.imgsz_var.get(),
            "blur_strength": self.blur_strength_var.get(),
            "blur_type": self.blur_type_var.get(),
            "kdenlive_id": self.kdenlive_id_var.get(),
            "tail_buffer": self.tail_buffer_var.get(),
            "media_dir": self.media_dir_var.get(),
            "existing": self.existing_var.get(),
            "preview_sec": self.preview_sec_var.get(),
            "person_min_h": self.person_min_h_var.get(),
            "person_min_area": self.person_min_area_var.get(),
            "veh_min_h": self.veh_min_h_var.get(),
            "veh_min_area": self.veh_min_area_var.get(),
            "input_path": self.input_path.get(),
            "output_path": self.output_path.get()
        }

    def apply_settings_dict(self, d):
        if "mode" in d: self.mode_var.set(d["mode"])
        if "export_mode" in d: self.export_mode_var.set("mkv" if d["export_mode"] == "mkv" else "kdenlive")   # old "kdenlive-timeline" -> "kdenlive"
        if "model_name" in d: self.model_var.set(d["model_name"])
        if "device" in d: self.device_var.set(d["device"])
        if "conf" in d: self.conf_var.set(d["conf"])
        if "imgsz" in d: self.imgsz_var.set(d["imgsz"])
        if "blur_strength" in d: self.blur_strength_var.set(d["blur_strength"])
        if "blur_type" in d: self.blur_type_var.set(d["blur_type"])
        if "kdenlive_id" in d: self.kdenlive_id_var.set(d["kdenlive_id"])
        if "tail_buffer" in d: self.tail_buffer_var.set(d["tail_buffer"])
        if "media_dir" in d: self.media_dir_var.set(d["media_dir"])
        if "existing" in d: self.existing_var.set(d["existing"])
        if "preview_sec" in d: self.preview_sec_var.set(str(d["preview_sec"]))
        if "person_min_h" in d: self.person_min_h_var.set(d["person_min_h"])
        if "person_min_area" in d: self.person_min_area_var.set(d["person_min_area"])
        if "veh_min_h" in d: self.veh_min_h_var.set(d["veh_min_h"])
        if "veh_min_area" in d: self.veh_min_area_var.set(d["veh_min_area"])
        if "input_path" in d: self.input_path.set(d["input_path"])
        if "output_path" in d: self.output_path.set(d["output_path"])

    def save_current_settings(self, silent=False):
        try:
            with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.get_settings_dict(), f, indent=4)
            if not silent:
                messagebox.showinfo("Success", f"Settings successfully saved to default location:\n{CONFIG_FILE_PATH}")
        except Exception as e:
            if not silent:
                messagebox.showerror("Error", f"Failed to save settings: {str(e)}")

    def load_settings_from_disk(self, path, silent=False):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    self.apply_settings_dict(d)
                if hasattr(self, "input_label"):
                    self.apply_mode_ui()
                if not silent:
                    messagebox.showinfo("Success", f"Settings loaded from {path}")
            except Exception as e:
                if not silent:
                    messagebox.showerror("Error", f"Failed to load settings: {str(e)}")

    def export_settings_file(self):
        filename = filedialog.asksaveasfilename(title="Export Settings Profile", defaultextension=".json", filetypes=[("JSON Config", "*.json")])
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(self.get_settings_dict(), f, indent=4)
                messagebox.showinfo("Success", "Settings exported successfully!")
            except Exception as e:
                messagebox.showerror("Error", f"Export failed: {str(e)}")

    def import_settings_file(self):
        filename = filedialog.askopenfilename(title="Import Settings Profile", filetypes=[("JSON Config", "*.json")])
        if filename:
            self.load_settings_from_disk(filename, silent=False)

    def load_yolo_model(self):
        chosen_model = self.model_var.get()
        device_choice = self.device_var.get()
        
        if device_choice.startswith("intel:"):
            base_name, _ = os.path.splitext(chosen_model)
            ov_dir = f"{base_name}_openvino_model"
            if not os.path.exists(ov_dir):
                self.status_var.set(f"Status: Exporting {chosen_model} to OpenVINO format for Intel hardware...")
                temp_model = YOLO(chosen_model)
                temp_model.export(format="openvino", half=True)
            return YOLO(ov_dir, task="detect")
        else:
            return YOLO(chosen_model, task="detect")

    def parse_time_to_frame(self, t_str, fps, max_frames, is_end=False):
        t_str = t_str.strip()
        if not t_str:
            return max_frames if is_end else 0
        try:
            if ":" in t_str:
                parts = list(map(float, t_str.split(":")))
                if len(parts) == 3:
                    sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                elif len(parts) == 2:
                    sec = parts[0] * 60 + parts[1]
                else:
                    sec = parts[0]
            else:
                sec = float(t_str)
            f = int(sec * fps)
            return max(0, min(f, max_frames))
        except Exception:
            return max_frames if is_end else 0

    def format_seconds_to_timecode(self, sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:05.2f}"
        else:
            return f"{m:02d}:{s:05.2f}"

    def update_preview_image(self, frame, detections):
        preview = frame.copy()
        for box, label in detections:
            x1, y1, x2, y2 = box
            color = (0, 0, 255) if "person" in label else (0, 255, 255)
            cv2.rectangle(preview, (x1, y1), (x2, y2), color, 2)
            cv2.putText(preview, label, (x1, max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        preview_rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(preview_rgb)
        img_pil.thumbnail((960, 540))
        
        self.photo_ref = ImageTk.PhotoImage(img_pil)
        
        if self.preview_text_id:
            self.preview_canvas.delete(self.preview_text_id)
            self.preview_text_id = None
            
        if self.preview_image_id is None:
            self.preview_image_id = self.preview_canvas.create_image(480, 270, anchor=tk.CENTER, image=self.photo_ref)
        else:
            self.preview_canvas.itemconfig(self.preview_image_id, image=self.photo_ref)

    def start_processing_thread(self):
        inp = self.input_path.get()
        out = self.output_path.get()
        export_mode = self.export_mode_var.get()

        if not inp or not out:
            messagebox.showerror("Error", "Please select an input file and an output target path.")
            return
        if os.path.abspath(inp) == os.path.abspath(out):
            messagebox.showerror("Error", "The output must be a different file than the input.")
            return

        target = self.process_video_task
        if export_mode != "mkv":
            if ka is None:
                messagebox.showerror("Error", "kdenlive_anonymize.py must be in the same folder as this script.")
                return
            if not inp.lower().endswith(".kdenlive") or not os.path.exists(inp):
                messagebox.showerror("Error", "Please select an existing .kdenlive project as input.")
                return
            target = self.process_timeline_task

        if os.path.exists(out) and not messagebox.askyesno("Overwrite?", f"{os.path.basename(out)} already exists.\nOverwrite it?"):
            return

        self.save_current_settings(silent=True)

        self.cancel_event.clear()
        self.running = True
        self.run_btn.config(state="normal", text="Cancel & Save Progress", bg="#c62828")
        self.status_var.set("Status: Initializing model and scanning video...")
        threading.Thread(target=target, daemon=True).start()

    def on_run_button(self):
        """The same button starts a run and - while it runs - stops it and saves what is done so far."""
        if self.running:
            self.cancel_event.set()
            self.run_btn.config(state="disabled", text="Stopping - saving progress...")
            self.status_var.set("Status: Stopping after the current frame, then saving...")
        else:
            self.start_processing_thread()

    def finish_run(self):
        self.running = False
        self.run_btn.config(state="normal", text="Start Processing & Generation", bg="#4CAF50")

    def get_preview_frames(self, fps):
        """Preview interval in frames, from the 'seconds' field (decimals allowed, 0 = off)."""
        try:
            sec = float(self.preview_sec_var.get().strip().replace(",", "."))
        except ValueError:
            sec = 1.0
        if sec <= 0:
            return 0
        return max(1, int(round(sec * fps)))

    def collect_detections(self, results, frame_idx, width, height, key_prefix, track_history, unique_tracks):
        """Turns one YOLO result into padded track boxes (size filters applied). Shared by all modes."""
        frame_detections = []
        if results.boxes is not None and results.boxes.id is not None:
            boxes = results.boxes.xyxy.cpu().numpy()
            ids = results.boxes.id.cpu().numpy().astype(int)
            classes = results.boxes.cls.cpu().numpy().astype(int)

            for box, tid, cls_id in zip(boxes, ids, classes):
                x1, y1, x2, y2 = map(int, box)
                w_box = x2 - x1
                h_box = y2 - y1

                min_h = self.person_min_h_var.get() if cls_id in [0, 1, 3] else self.veh_min_h_var.get()
                min_area = self.person_min_area_var.get() if cls_id in [0, 1, 3] else self.veh_min_area_var.get()

                if h_box < min_h or (w_box * h_box) < min_area:
                    continue

                cls_name = "person" if cls_id == 0 else "vehicle"
                key = f"{key_prefix}{cls_name}_{tid}"
                unique_tracks.add(f"{key_prefix}{tid}")

                px = int(w_box * 0.30)
                py = int(h_box * 0.30)
                x1 = max(0, x1 - px)
                y1 = max(0, y1 - py)
                x2 = min(width, x2 + px)
                y2 = min(height, y2 + py)

                coord = (x1, y1, x2 - x1, y2 - y1)
                if key not in track_history: track_history[key] = []
                track_history[key].append((frame_idx, *coord))
                frame_detections.append(((x1, y1, x2, y2), f"{cls_name.capitalize()} #{tid}"))
        return frame_detections

    def reset_tracker(self, model):
        """Forget all tracks so IDs from the previous frame range cannot leak into the next one."""
        pred = getattr(model, "predictor", None)
        done = False
        for t in (getattr(pred, "trackers", None) or []):
            try:
                t.reset()
                done = True
            except Exception:
                pass
        if not done:
            model.predictor = None      # forces a clean tracker on the next model.track() call

    def process_timeline_task(self):
        """Timeline mode: anonymize an existing, already cut Kdenlive project."""
        try:
            project_path = self.input_path.get()
            output_path = self.output_path.get()
            media_dir = self.media_dir_var.get().strip() or None
            mode = self.mode_var.get()
            device_choice = self.device_var.get()
            conf = self.conf_var.get()
            imgsz = self.imgsz_var.get()
            tail_buffer = self.tail_buffer_var.get()

            if mode == "persons":
                target_classes = [0]
            elif mode == "vehicles":
                target_classes = [1, 2, 3, 5, 7]
            else:
                target_classes = [0, 1, 2, 3, 5, 7]

            opts = ka.Options(blur_strength=self.blur_strength_var.get(), blur_type=self.blur_type_var.get(),
                              kdenlive_id=self.kdenlive_id_var.get(), existing=self.existing_var.get())

            self.status_var.set("Status: Reading project and planning the frame ranges that are really used...")
            proj = ka.KdenliveProject(project_path)

            # optional time range, given as timeline time (like in Kdenlive's timeline ruler)
            window = None
            start_txt, end_txt = self.start_time_var.get().strip(), self.end_time_var.get().strip()
            if start_txt or end_txt:
                ws = ka.parse_user_time(start_txt, proj.fps) if start_txt else 0
                we = ka.parse_user_time(end_txt, proj.fps) if end_txt else None
                if ws is None or (end_txt and we is None):
                    raise Exception("Could not read the time range. Please use HH:MM:SS (e.g. 00:09:00 and 00:10:30).")
                if we is not None and we < ws:
                    raise Exception("The end of the time range lies before its start.")
                window = (ws, we)

            schedule, skipped, kept = proj.plan_schedule(media_dir=media_dir, existing=opts.existing, window=window)
            if not schedule:
                raise Exception("Nothing to analyse. No video clips were found in the selected time range on the video tracks, "
                                "or all of them already have a tracker effect (see 'Existing Tracker Effects').")
            missing = sorted({it[0] for it in schedule if not os.path.exists(it[0])})
            if missing:
                raise Exception("Source video(s) not found:\n" + "\n".join(missing[:5]) +
                                "\n\nSet 'Media Folder' to the folder that contains them.")
            total_to_scan = sum(b - a + 1 for _, a, b, _ in schedule)
            last_frame_tl = window[1] if (window and window[1] is not None) else proj.timeline_end_frame()
            end_ts = ka.format_clock(last_frame_tl / proj.fps)

            model = self.load_yolo_model()
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "hw_accel;vaapi"

            tracks_by_path = {}
            range_count = {}
            unique_tracks = set()
            done = 0
            start_time = time.time()
            cancelled = False
            last_tl = None

            # processed in timeline order, so a stopped run can be continued from a timestamp
            for path, a, b, _tl in schedule:
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG, (cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY))
                if not cap.isOpened():
                    cap = cv2.VideoCapture(path)
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                preview_interval = self.get_preview_frames(fps)
                track_history = tracks_by_path.setdefault(path, {})
                ri = range_count.get(path, 0)
                range_count[path] = ri + 1

                self.reset_tracker(model)
                cap.set(cv2.CAP_PROP_POS_FRAMES, a)
                frame_idx = a
                while frame_idx <= b:
                    if self.cancel_event.is_set():
                        cancelled = True
                        break
                    ret, frame = cap.read()
                    if not ret:
                        break
                    results = model.track(frame, persist=True, conf=conf, imgsz=imgsz, classes=target_classes, device=device_choice, verbose=False)[0]
                    frame_detections = self.collect_detections(results, frame_idx, width, height, f"r{ri}_", track_history, unique_tracks)
                    tl_now = proj.timeline_frame(path, frame_idx)
                    if tl_now is not None:
                        last_tl = tl_now
                    frame_idx += 1
                    done += 1

                    if done % 15 == 0:
                        elapsed = time.time() - start_time
                        fps_proc = done / elapsed if elapsed > 0 else 1
                        eta_sec = (total_to_scan - done) / fps_proc if fps_proc > 0 else 0
                        mins, secs = divmod(int(eta_sec), 60)
                        hours, mins = divmod(mins, 60)
                        eta_str = f"{hours:02d}:{mins:02d}:{secs:02d}" if hours > 0 else f"{mins:02d}:{secs:02d}"
                        tl_ts = ka.format_clock((last_tl or 0) / proj.fps)
                        src_ts = ka.format_clock(frame_idx / fps)
                        self.status_var.set(f"Timeline [{tl_ts} / {end_ts}] | {os.path.basename(path)} @ {src_ts} | "
                                            f"{done}/{total_to_scan} frames | {fps_proc:.1f} fps")
                        self.counts_var.set(f"Detections (Unique Tracks): {len(unique_tracks)}")
                        self.eta_var.set(f"Estimated Time Remaining: {eta_str}")
                        preview_interval = self.get_preview_frames(fps)     # may be changed while running

                    if preview_interval and done % preview_interval == 0:
                        self.root.after(0, lambda f=frame.copy(), d=frame_detections: self.update_preview_image(f, d))
                cap.release()
                if cancelled:
                    break

            for th in tracks_by_path.values():
                ka.extend_tails(th, tail_buffer)

            if cancelled and done == 0:
                self.status_var.set("Status: Cancelled before anything was processed - nothing saved.")
                messagebox.showinfo("Cancelled", "Cancelled before anything was processed. Nothing was saved.")
                return

            self.counts_var.set(f"Detections (Unique Tracks): {len(unique_tracks)}")
            self.eta_var.set("Estimated Time Remaining: Stopped" if cancelled else "Estimated Time Remaining: Complete!")
            self.status_var.set("Status: Inserting blur filters into the project timeline...")
            stats = proj.apply_tracks(tracks_by_path, opts, media_dir=media_dir, window=window)
            proj.save(output_path)

            lines = [f"Clips on video tracks: {stats['clips']}",
                     f"Anonymized: {stats['processed']}  (pieces: {stats['pieces']}, filters: {stats['filters']})",
                     f"Left untouched (existing tracker effect): {stats['kept_existing']}",
                     f"No detections: {stats['no_detections']}"]
            if window is not None:
                lines.append(f"Outside the time range (not touched): {stats['outside_range']}")
            if proj.warnings:
                lines += ["", "Warnings:"] + sorted(set(proj.warnings))[:8]

            if cancelled:
                resume_ts = ka.format_clock((last_tl or 0) / proj.fps)
                self.status_var.set(f"Status: Stopped at timeline {resume_ts} - progress saved.")
                saved_to = output_path
                # prepare the next run: continue in the saved project, starting at the stop position
                self.input_path.set(saved_to)
                self.start_time_var.set(resume_ts)
                self.suggest_output()
                messagebox.showinfo("Stopped & saved",
                                    f"Stopped at timeline position {resume_ts}. Progress saved to:\n{saved_to}\n\n"
                                    f"To continue: the saved project is now the input and the start time is set to {resume_ts} - "
                                    f"just press Start again (the end time was left as it is).\n\n" + "\n".join(lines))
            else:
                self.status_var.set("Status: Done! Generation completed successfully.")
                messagebox.showinfo("Success", "Project written to:\n" + output_path + "\n\n" + "\n".join(lines))

        except Exception as e:
            self.status_var.set("Status: Error during processing.")
            messagebox.showerror("Error", f"An error occurred:\n{str(e)}")
        finally:
            self.finish_run()

    def process_video_task(self):
        try:
            if not shutil.which("ffmpeg"):
                raise Exception("ffmpeg was not found. It is needed to add the original audio to the check video.\n\nInstall it with:\n  " + system_package_hint("ffmpeg"))
            video_path = self.input_path.get()
            output_path = self.output_path.get()
            
            mode = self.mode_var.get()
            export_mode = self.export_mode_var.get()
            device_choice = self.device_var.get()
            conf = self.conf_var.get()
            imgsz = self.imgsz_var.get()
            tail_buffer = self.tail_buffer_var.get()
            blur_intensity = self.blur_strength_var.get()

            if mode == "persons":
                target_classes = [0]
            elif mode == "vehicles":
                target_classes = [1, 2, 3, 5, 7]
            else:
                target_classes = [0, 1, 2, 3, 5, 7]

            model = self.load_yolo_model()

            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "hw_accel;vaapi"
            cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG, (cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY))
            if not cap.isOpened():
                cap = cv2.VideoCapture(video_path)

            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            start_frame = self.parse_time_to_frame(self.start_time_var.get(), fps, total_frames, is_end=False)
            end_frame = self.parse_time_to_frame(self.end_time_var.get(), fps, total_frames, is_end=True)

            if start_frame > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
                frame_idx = start_frame
            else:
                frame_idx = 0

            track_history = {}
            unique_tracks = set()
            preview_interval = self.get_preview_frames(fps)
            start_time = time.time()
            cancelled = False

            # ================= STAGE 1: SCAN & TRACK =================
            while cap.isOpened():
                if frame_idx > end_frame:
                    break
                if self.cancel_event.is_set():
                    cancelled = True
                    end_frame = max(start_frame, frame_idx - 1)     # render only what has been analysed so far
                    break
                
                ret, frame = cap.read()
                if not ret:
                    break
                
                results = model.track(frame, persist=True, conf=conf, imgsz=imgsz, classes=target_classes, device=device_choice, verbose=False)[0]
                frame_detections = self.collect_detections(results, frame_idx, width, height, "", track_history, unique_tracks)

                frame_idx += 1

                if frame_idx % 15 == 0:
                    elapsed = time.time() - start_time
                    fps_proc = (frame_idx - start_frame) / elapsed if elapsed > 0 else 1
                    remaining_frames = end_frame - frame_idx
                    eta_sec = remaining_frames / fps_proc if fps_proc > 0 else 0
                    
                    mins, secs = divmod(int(eta_sec), 60)
                    hours, mins = divmod(mins, 60)
                    eta_str = f"{hours:02d}:{mins:02d}:{secs:02d}" if hours > 0 else f"{mins:02d}:{secs:02d}"

                    curr_sec = frame_idx / fps if fps > 0 else 0
                    total_sec = end_frame / fps if fps > 0 else 0
                    curr_ts = self.format_seconds_to_timecode(curr_sec)
                    total_ts = self.format_seconds_to_timecode(total_sec)

                    self.status_var.set(f"Processing: [{curr_ts} / {total_ts}] | Frame {frame_idx}/{end_frame} | {fps_proc:.1f} fps")
                    self.counts_var.set(f"Detections (Unique Tracks): {len(unique_tracks)}")
                    self.eta_var.set(f"Estimated Time Remaining: {eta_str}")

                    preview_interval = self.get_preview_frames(fps)      # may be changed while running

                if preview_interval and frame_idx % preview_interval == 0:
                    self.root.after(0, lambda f=frame.copy(), d=frame_detections: self.update_preview_image(f, d))

            cap.release()

            if cancelled and frame_idx <= start_frame:
                self.status_var.set("Status: Cancelled before anything was processed - nothing saved.")
                messagebox.showinfo("Cancelled", "Cancelled before anything was processed. Nothing was saved.")
                return

            # --- Apply Tail Buffer Extension ---
            if tail_buffer > 0:
                for track_key, coords in track_history.items():
                    if not coords:
                        continue
                    coords.sort(key=lambda x: x[0])
                    last_f, lx, ly, lw, lh = coords[-1]
                    for extra_f in range(1, tail_buffer + 1):
                        ext_f = last_f + extra_f
                        if ext_f <= end_frame:
                            coords.append((ext_f, lx, ly, lw, lh))

            self.counts_var.set(f"Detections (Unique Tracks): {len(unique_tracks)}")
            self.eta_var.set("Estimated Time Remaining: Complete!")

            # ================= STAGE 2: EXPORT MODE =================
            self.status_var.set("Status: Rendering direct MKV verification video with audio...")
            self.render_direct_mkv(video_path, output_path, track_history, blur_intensity, width, height, fps, start_frame, end_frame)

            if cancelled:
                resume_ts = ka.format_clock(frame_idx / fps)
                self.start_time_var.set(resume_ts)
                self.status_var.set(f"Status: Stopped at {resume_ts} - rendered up to there.")
                messagebox.showinfo("Stopped & saved", f"Stopped at {resume_ts}. Check video rendered up to there:\n{output_path}\n\n"
                                                       f"The start time is now set to {resume_ts} for the next run.")
            else:
                self.status_var.set("Status: Done! Generation completed successfully.")
                messagebox.showinfo("Success", f"Output generated successfully!\nSaved to: {output_path}")

        except Exception as e:
            self.status_var.set("Status: Error during processing.")
            messagebox.showerror("Error", f"An error occurred:\n{str(e)}")
        finally:
            self.finish_run()

    def render_direct_mkv(self, video_path, output_mkv_path, track_history, blur_intensity, width, height, fps, start_frame, end_frame):
        frame_box_map = {}
        for track_key, coords in track_history.items():
            for f_num, x, y, w, h in coords:
                if f_num not in frame_box_map:
                    frame_box_map[f_num] = []
                frame_box_map[f_num].append((x, y, w, h))

        cap = cv2.VideoCapture(video_path)
        if start_frame > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            f_idx = start_frame
        else:
            f_idx = 0

        base_dir = os.path.dirname(output_mkv_path)
        temp_video_path = os.path.join(base_dir, "_temp_blur_video.avi")
        
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out_writer = cv2.VideoWriter(temp_video_path, fourcc, fps, (width, height))

        total_to_render = (end_frame - start_frame) + 1
        rendered_count = 0

        # Map Kdenlive blur types to OpenCV blur implementations for verification preview/render
        blur_type_choice = self.blur_type_var.get()

        while cap.isOpened() and f_idx <= end_frame:
            ret, frame = cap.read()
            if not ret:
                break

            if f_idx in frame_box_map:
                for (x, y, w, h) in frame_box_map[f_idx]:
                    if w <= 0 or h <= 0:
                        continue
                    x1 = max(0, x)
                    y1 = max(0, y)
                    x2 = min(width, x + w)
                    y2 = min(height, y + h)
                    
                    if x2 > x1 and y2 > y1:
                        roi = frame[y1:y2, x1:x2]
                        if blur_type_choice == "Opaque Fill":
                            roi[:] = (0, 0, 0) # Solid black mask
                        elif blur_type_choice == "Pixalate":
                            h_roi, w_roi = roi.shape[:2]
                            pixel_scale = max(2, blur_intensity // 2)
                            small = cv2.resize(roi, (max(1, w_roi // pixel_scale), max(1, h_roi // pixel_scale)), interpolation=cv2.INTER_LINEAR)
                            roi[:] = cv2.resize(small, (w_roi, h_roi), interpolation=cv2.INTER_NEAREST)
                        else: # Gaussian or Median
                            ksize = (max(1, (blur_intensity * 2 + 1)), max(1, (blur_intensity * 2 + 1)))
                            if blur_type_choice == "Median Blur":
                                roi = cv2.medianBlur(roi, ksize[0] if ksize[0] % 2 == 1 else ksize[0] + 1)
                            else:
                                roi = cv2.GaussianBlur(roi, ksize, 0)
                        frame[y1:y2, x1:x2] = roi

            out_writer.write(frame)
            f_idx += 1
            rendered_count += 1

            if rendered_count % 30 == 0:
                curr_time_str = self.format_seconds_to_timecode(f_idx / fps)
                total_time_str = self.format_seconds_to_timecode(end_frame / fps)
                self.status_var.set(f"Rendering: [{curr_time_str} / {total_time_str}] | Frame {rendered_count}/{total_to_render}")

        cap.release()
        out_writer.release()

        self.status_var.set("Status: Muxing audio track via ffmpeg...")
        start_sec = start_frame / fps if fps > 0 else 0
        duration_sec = total_to_render / fps if fps > 0 else 10

        mux_success = False
        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", video_path,
                "-i", temp_video_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "1:v:0",
                "-map", "0:a:0?",
                "-t", str(duration_sec),
                output_mkv_path
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                mux_success = True
        except Exception:
            pass

        if os.path.exists(temp_video_path):
            if not mux_success:
                if os.path.exists(output_mkv_path):
                    os.remove(output_mkv_path)
                os.rename(temp_video_path, output_mkv_path)
            else:
                os.remove(temp_video_path)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".kdenlive"):
        # dry run without GUI: python kdenlive_blur_gui.py project.kdenlive [media_folder] [start] [end]
        sys.exit(plan_cli(sys.argv))
    def get_config_dir():
        if platform.system() == "Windows":
            base_dir = os.path.expandvars(r"%APPDATA%\KdenliveBlurApp")
        else:
            base_dir = os.path.expanduser("~/.config/kdenlive_blur_gui")
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    CONFIG_FILE_PATH = os.path.join(get_config_dir(), "settings.json")

    root = tk.Tk(className="Kdenlive-blur-gui")      # WM class used by the desktop launcher to match the icon
    try:                                              # window / taskbar icon (installed next to the script)
        _icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), APP_ID + ".png")
        if os.path.exists(_icon):
            root.iconphoto(True, tk.PhotoImage(file=_icon))
    except Exception:
        pass
    app = BlurApp(root)
    root.mainloop()