#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Dietmar Schnabel <dietmar.schnabel<at>gmail.com>
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU Affero General Public License as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version. It is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License (file LICENSE) for details.
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
import traceback

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
_OK_MARKER = os.path.join(VENV_DIR, ".components_ok")     # written when the private environment is known to be complete
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0         # subprocess flag: never pop up a console window on Windows
MODELS_DIR = os.path.join(_data_dir(), "models")      # YOLO weights (and their OpenVINO export) live here
_LOG_PATH = None


def _ensure_std_streams():
    """Windows shortcuts start the tool with pythonw.exe: no console, so sys.stdout and sys.stderr are None.
    Libraries that print progress bars (the model download of Ultralytics, pip, ...) then crash with
    "'NoneType' object has no attribute 'write'". Send the output to a log file instead."""
    global _LOG_PATH
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        path = os.path.join(_data_dir(), "app.log")
        if os.path.exists(path) and os.path.getsize(path) > 1000000:
            os.replace(path, path + ".old")
        log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        log.write("\n--- %s %s started %s ---\n" % (APP_NAME, __version__, time.strftime("%Y-%m-%d %H:%M:%S")))
        _LOG_PATH = path
    except OSError:
        log = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


_ensure_std_streams()


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


def describe_error(e):
    """Text for the error dialog: the message, a hint for the usual download problem, and where the log is."""
    text = str(e)
    msg = "An error occurred:\n%s" % text
    low = text.lower()
    if "download failure" in low or "retry limit" in low:
        msg += ("\n\nThe YOLO model file could not be downloaded. Check your internet connection and firewall and try again.\n"
                "Or download the file yourself from https://github.com/ultralytics/assets/releases and put it into:\n  " + MODELS_DIR)
    if _LOG_PATH:
        msg += "\n\nMore details: " + _LOG_PATH
    return msg


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
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1")
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors="replace", env=env, creationflags=_NO_WINDOW)
    for line in p.stdout:
        log(line.rstrip())
    return p.wait()


def install_dependencies(variant, log):
    """variant 'cpu': CPU build of PyTorch (small, also used for Intel GPUs through OpenVINO).
    variant 'default': whatever PyPI offers (on Linux a large CUDA build for NVIDIA cards)."""
    os.makedirs(_data_dir(), exist_ok=True)
    if os.path.exists(_OK_MARKER):
        os.remove(_OK_MARKER)
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
    with open(_OK_MARKER, "w", encoding="utf-8") as f:
        f.write(__version__)
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
                         "To repair it run in a terminal:\n  %s kdenlive_blur_gui.py --install-deps" % (reason, hint, "py" if os.name == "nt" else "python3"))
    if os.path.exists(_venv_python()):
        if os.path.exists(_OK_MARKER):
            _relaunch_in_venv()                    # known to be complete: no slow check, no flashing console window
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "creationflags": _NO_WINDOW}
        if subprocess.call([_venv_python(), "-c", "import cv2, PIL.ImageTk, ultralytics"], **quiet) == 0:
            try:
                with open(_OK_MARKER, "w", encoding="utf-8") as f:     # remember it for the next start
                    f.write(__version__)
            except OSError:
                pass
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

import bisect
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
                 existing="keep", merge_gap=15, split_audio=True, thin_pct=4.0):
        self.blur_strength = int(blur_strength)
        self.blur_type_val = BLUR_TYPE_VALUES.get(blur_type, "2")
        self.kdenlive_id = (kdenlive_id or "").strip() or "opencv.tracker"
        # existing: what to do with clips that already carry an opencv.tracker filter
        #   "keep"    -> leave them completely alone (default, protects manual work)
        #   "replace" -> remove the old tracker filters when new detections exist
        self.existing = existing
        self.merge_gap = int(merge_gap)
        self.split_audio = bool(split_audio)
        # keyframe thinning: drop keyframes the interpolation reproduces within this % of the object size (0 = keep all)
        self.thin_pct = float(thin_pct)


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
        dense = len(coords)
        coords = thin_keyframes(coords, opts.thin_pct)
        self._kf_dense += dense
        self._kf_kept += len(coords)
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
        self._kf_dense = self._kf_kept = 0
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
        stats["keyframes"], stats["keyframes_dense"] = self._kf_kept, self._kf_dense
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

    # ------------------------------------------------- merge the effects of another project into this one
    @staticmethod
    def _norm_path(p):
        return os.path.normpath((p or "").replace("\\", "/")).replace("\\", "/").lower()

    def _media_keys(self, e):
        """(full normalized path, lower-case file name) of the media file a clip entry uses."""
        raw = e.raw_path
        is_abs = os.path.isabs(raw) or bool(re.match(r"^[A-Za-z]:[\\/]", raw))
        full = raw if is_abs else os.path.join(self.media_root, raw)
        return self._norm_path(full), os.path.basename(raw.replace("\\", "/")).lower()

    @staticmethod
    def _wanted_filter(f, scope):
        service = _prop(f, "mlt_service", "")
        if service == TRACKER_SERVICE:
            return True
        # other effects: only simple ones (a filter with its own in/out would not keep its meaning on a different cut)
        return scope == "all" and bool(service) and not (f.get("in") or f.get("out"))

    def media_with_effects(self, scope="blur"):
        """Media files that carry effects in this project: [(key, name, number of effects, first frame, last frame)]."""
        out = {}
        for e in self.list_entries(video_only=True):
            if e.raw_path is None:
                continue
            n = len([f for f in e.el.findall("filter") if self._wanted_filter(f, scope)])
            if n:
                rec = out.setdefault(self._media_keys(e)[0], [os.path.basename(e.raw_path.replace("\\", "/")), 0, e.in_f, e.out_f])
                rec[1] += n
                rec[2], rec[3] = min(rec[2], e.in_f), max(rec[3], e.out_f)
        return sorted(((k, v[0], v[1], v[2], v[3]) for k, v in out.items()), key=lambda r: -r[2])

    def video_media(self):
        """Media files used on the video tracks: [(key, name, number of clips)]."""
        out = {}
        for e in self.list_entries(video_only=True):
            if e.raw_path is not None:
                rec = out.setdefault(self._media_keys(e)[0], [os.path.basename(e.raw_path.replace("\\", "/")), 0])
                rec[1] += 1
        return sorted(((k, v[0], v[1]) for k, v in out.items()), key=lambda r: r[1].lower())

    def video_tracks(self):
        """Video tracks that hold clips: [(track id, number of clips)]."""
        out = {}
        for e in self.list_entries(video_only=True):
            out[e.track.id] = out.get(e.track.id, 0) + 1
        return sorted(out.items())

    def _frames_prop(self, el, name):
        """Value of a property as frames (Kdenlive writes lengths as a frame count, sometimes as a clock time)."""
        v = (_prop(el, name, "") if el is not None else "") or ""
        try:
            return self.parse_time(v) if ":" in v else int(float(v))
        except ValueError:
            return 0

    def footage_axis(self):
        """How this project's timeline relates to a CONTINUOUS recording, when cuts inside a file are undone.
        Example: a clip of one file was cut in two pieces and 702 frames between them were thrown away. A video that was
        rendered BEFORE that cut still contains those frames, so its time axis runs 702 frames ahead of the timeline
        from there on. Returns (offsets, total_shift, timeline_length, main_track_id) where offsets[media key] is the
        position on that 'continuous' axis at which frame 0 of the file lies."""
        entries = [e for e in self.list_entries(video_only=True) if e.raw_path is not None]
        if not entries:
            return {}, 0, 0, None
        per_track = {}
        for e in entries:
            per_track[e.track.id] = per_track.get(e.track.id, 0) + 1
        main = max(per_track, key=lambda t: (per_track[t], -t))        # the track that carries the story
        clips = sorted((e for e in entries if e.track.id == main), key=lambda e: e.pos)
        shift, steps = 0, [(clips[0].pos, 0)]
        for a, b in zip(clips, clips[1:]):
            if self._media_keys(a)[0] == self._media_keys(b)[0]:
                gap = b.in_f - (a.out_f + 1)                           # footage skipped between the two pieces of one file
                if gap > 0:
                    shift += gap - (b.pos - (a.pos + a.length))        # minus blank frames left on the timeline
            steps.append((b.pos, shift))

        def shift_at(pos):
            last = 0
            for p, sh in steps:
                if p > pos:
                    break
                last = sh
            return last

        offsets = {}
        for e in sorted(entries, key=lambda e: (e.track.id != main, e.pos)):   # main track first, then the others
            offsets.setdefault(self._media_keys(e)[0], e.pos + shift_at(e.pos) - e.in_f)
        return offsets, shift, max(c.pos + c.length for c in clips), main

    def plan_merge(self, source, scope="blur", existing="keep", mode="pair", src_media=None, dst_media=None, offset=0, track=None,
                   restore_cuts="auto", same_files=True):
        """Plans copying the effects of the project 'source' (and the cuts they need) onto clips of THIS project.
        The effects are tied to the TIME in the source video, so the target may use different files.
          mode 'pair': source video -> target video file. Automatic by file path / unique file name, or manual: the
                       source video 'src_media' goes to the file 'dst_media' (keys from media_with_effects/video_media).
                       Target file time = source video time + offset (frames). Stops where the target file ends.
          mode 'time': source time 0 sits at timeline position 'offset' (frames) of this project; the effects
                       go onto whatever clips are there (all video tracks, or only track id 'track').
                       A source with ONE video (a rendered edit): that video's own time is the axis. A source with SEVERAL
                       videos / tracks: every video is placed where the source project puts it, and the effects of ALL
                       of them are copied (src_media=None) or only those of one video.
                       Stops where the target has no clips any more.
                       same_files: a source video that is ALSO used in the target (same path, or same unique file name)
                       is copied exactly by file - its effects land on the same frames of that file, whatever the cuts and
                       positions are (needs offset 0). Only the other source videos are placed by the timeline.
                       restore_cuts: the source video may have been rendered BEFORE the target got cuts inside its files
                       (footage thrown away) - then the video still contains that footage. 'yes' = undo such cuts for the
                       mapping, 'no' = use the timeline as it is, 'auto' = decide by comparing the video's length with
                       the target's timeline (only when offset is 0).
        scope 'blur' = tracker (blur) effects only, 'all' = also other simple effects.
        Nothing is changed here. Returns (plan, report_lines)."""
        ann, bases, src_len = {}, {}, {}         # full path -> [(first, last, [filters], name)]; file name -> {full paths}; video lengths
        n_src_filters = 0
        for e in source.list_entries(video_only=True):
            if e.raw_path is None:
                continue
            flts = [f for f in e.el.findall("filter") if self._wanted_filter(f, scope)]
            full, base = source._media_keys(e)
            if not flts or (src_media is not None and full != src_media):
                continue
            a, b = self.parse_time(e.el.get("in")), self.parse_time(e.el.get("out"))     # video time, as frames of THIS project
            src_len[full] = max(src_len.get(full, 0), self._frames_prop(source.by_id.get(e.el.get("producer")), "length"), b + 1)
            ann.setdefault(full, []).append((a, b, flts, os.path.basename(e.raw_path.replace("\\", "/"))))
            bases.setdefault(base, set()).add(full)
            n_src_filters += len(flts)
        ident = {}                                 # source video -> the same file in the target (copied exactly by file)
        if mode == "time" and ann and same_files and offset == 0:
            tkeys = {k for k, n, c in self.video_media()}
            tbase = {}
            for k in tkeys:
                tbase.setdefault(os.path.basename(k), set()).add(k)
            for skey in ann:
                cands = {skey} & tkeys or (tbase.get(os.path.basename(skey), set()) if len(tbase.get(os.path.basename(skey), ())) == 1 else set())
                if cands:
                    ident[skey] = next(iter(cands))
        timed = [k for k in ann if k not in ident]  # source videos that are placed by the timeline
        axis_src = {}                              # per such video: where its frame 0 lies on the source's time axis
        if mode == "time" and timed:
            all_media = {source._media_keys(e)[0] for e in source.list_entries(video_only=True) if e.raw_path is not None}
            if len(all_media) <= 1:
                axis_src = {k: 0 for k in timed}   # one video (e.g. a rendered edit): its own time IS the axis
            else:                                  # several videos / tracks: each lies where the source project puts it
                src_offsets = source.footage_axis()[0]
                axis_src = {k: int(round(src_offsets.get(k, 0) / source.fps * self.fps)) for k in timed}

        axis_report = []
        file_offsets = {}
        restore = False
        if mode == "time" and timed:
            file_offsets, total_shift, tl_len, main_track = self.footage_axis()
            if total_shift > 0:
                if restore_cuts == "yes":
                    restore = True
                elif restore_cuts == "auto" and offset == 0 and any(k in timed for k in src_len):
                    L = max(axis_src.get(k, 0) + n for k, n in src_len.items() if k in timed)
                    restore = abs(L - (tl_len + total_shift)) < abs(L - tl_len)
                    axis_report = ["Source: %d frames long. Target timeline: %d frames; with the %d frames thrown away inside files "
                                   "put back: %d frames." % (L, tl_len, total_shift, tl_len + total_shift)]
                if restore:
                    axis_report.append("The target has cut footage out of its files that the source video still contains. "
                                       "Those cuts are taken into account (%d frames), so the effects stay in sync with the picture "
                                       "after them. Effects that lie in the removed footage are skipped." % total_shift)
                elif axis_report:
                    axis_report.append("The source video fits the target timeline as it is - the cuts inside files are used as they are.")

        plan, matched, kept, hit = [], set(), 0, set()
        per_file, n_clips = {}, 0
        for e in self.list_entries(video_only=True):
            if e.raw_path is None:
                continue
            n_clips += 1
            if mode == "time" and track is not None and e.track.id != track:
                continue
            full, base = self._media_keys(e)
            overlaps = []                                         # (first, last, effects, shift) in this clip's media time
            if mode == "time":
                key = None
                origin = (file_offsets[full] if restore else e.pos - e.in_f) if timed else 0   # where media frame 0 of this clip sits
                for skey, blocks in ann.items():                  # the effects of EVERY source video (all tracks)
                    if skey in ident:
                        if ident[skey] != full:
                            continue                              # the same file: media time is simply the same
                        base_t, org = 0, 0
                    else:
                        base_t, org = offset + axis_src.get(skey, 0), origin
                    for i, (a, b, flts, name) in enumerate(blocks):
                        lo, hi = max(base_t + a - org, e.in_f), min(base_t + b - org, e.out_f)
                        if lo <= hi:                              # the part of the effect that lies on this clip
                            overlaps.append((lo, hi, flts, base_t - org))
                            hit.add((skey, i))
            else:
                if dst_media is not None and src_media is not None:
                    if full != dst_media:
                        continue
                    key = src_media
                else:
                    key = full if full in ann else (next(iter(bases[base])) if len(bases.get(base, ())) == 1 else None)
                if key is None or key not in ann:
                    continue
                for i, (a, b, flts, name) in enumerate(ann[key]):
                    lo, hi = max(a + offset, e.in_f), min(b + offset, e.out_f)
                    if lo <= hi:
                        overlaps.append((lo, hi, flts, offset))
                        hit.add((key, i))
            if not overlaps:
                continue
            matched.add(key)
            if existing == "keep" and self._tracker_filters(e.el):
                kept += 1
                continue
            cuts = {e.in_f, e.out_f + 1}
            for lo, hi, _, _ in overlaps:
                cuts.update((lo, hi + 1))
            pts = sorted(cuts)
            pieces = []                                           # (first, last, indices of the effects active there)
            for p, q in zip(pts, pts[1:]):
                cov = frozenset(i for i, (lo, hi, _, _) in enumerate(overlaps) if lo <= p and q - 1 <= hi)
                if pieces and pieces[-1][2] == cov:
                    pieces[-1] = (pieces[-1][0], q - 1, cov)       # neighbours with the same effects stay one piece
                else:
                    pieces.append((p, q - 1, cov))
            foreign = [f for f in e.el.findall("filter") if f not in self._tracker_filters(e.el)]
            n_f = sum(len(overlaps[i][2]) for _, _, cov in pieces for i in cov)
            plan.append({"entry": e, "overlaps": overlaps, "pieces": pieces, "whole": bool(foreign)})
            st = per_file.setdefault(os.path.basename(e.raw_path.replace("\\", "/")), [0, 0, 0])
            st[0] += 1
            st[1] += len(pieces) if not foreign else 0
            st[2] += n_f if not foreign else sum(len(f) for _, _, f, _ in overlaps)

        n_blocks = sum(len(v) for v in ann.values())
        names = ", ".join(sorted({r[3] for v in ann.values() for r in v})) or "-"
        report = ["Source project: %d effect(s) on %d video part(s) of: %s." % (n_src_filters, n_blocks, names),
                  "Target project: %d video clip(s)." % n_clips]
        if mode == "time":
            if len(ann) > 1:
                report.append("Mode: by time - the source has %d videos (tracks); each one is placed on the target timeline like in "
                              "the source project%s; %s." % (len(ann), " + " + format_clock(offset / self.fps + 0.05) if offset else "",
                                                             "onto all video tracks" if track is None else "onto track %d only" % track))
            else:
                report.append("Mode: by time - the source video's time 0 sits at %s on the target timeline; %s." % (
                    format_clock(offset / self.fps + 0.05), "all video tracks" if track is None else "track %d only" % track))
            if ident:
                report.append("Same files: %d of %d source video(s) are also used in the target; their effects are copied exactly by "
                              "file (cuts and positions do not matter)%s." % (len(ident), len(ann), "; the others are placed by the timeline" if timed else ""))
        elif dst_media is not None:
            report.append("Mode: file pairing - the chosen source video goes onto the chosen target video (target time = source time %s)." % (
                "%+.2f s" % (offset / self.fps) if offset else "unchanged"))
        else:
            report.append("Mode: file pairing - source and target videos are matched by path or file name.")
        report += axis_report
        report.append("")
        if per_file:
            report.append("Clips in the target that receive effects:")
            for name, (clips, pieces, flts) in sorted(per_file.items()):
                report.append("  %s: %d clip(s) (%d piece(s), %d effect copies)" % (name, clips, pieces, flts))
        else:
            report.append("No clip of the target receives effects" + (" - nothing of the target lies at that time." if mode == "time"
                          else " - no matching video file; choose the target video yourself or switch to mode 'by time'."))
        dropped = [(key, i) for key, v in ann.items() for i in range(len(v)) if (key, i) not in hit]
        if dropped and per_file:
            first = min(ann[k][i][0] for k, i in dropped)
            report += ["", "%d of %d effect part(s) found no place in the target (it is shorter or has nothing there); "
                           "the first one is at %s of the source video. They are skipped." % (len(dropped), n_blocks, format_clock(first / self.fps + 0.05))]
        lost = sorted({ann[k][0][3] for k in ann if k not in matched}) if mode == "pair" and dst_media is None else []
        if lost:
            report += ["", "No matching video in the target for (their effects are NOT copied):"] + ["  " + n for n in lost]
        if kept:
            report += ["", "%d target clip(s) already have blur effects and are left untouched (see option 'existing')." % kept]
        if any(item["whole"] for item in plan):
            report += ["", "Clips that carry other effects are not cut: only blur effects are added on top of them."]
        return plan, report

    def _shift_results(self, flt, shift):
        """Moves all keyframes of a tracker effect by 'shift' frames (the target clip has another time offset)."""
        if not shift:
            return
        prop = next((p for p in flt.findall("property") if p.get("name") == "results"), None)
        if prop is None or not prop.text:
            return
        items = {}
        for kf in prop.text.split(";"):
            if kf:
                tc, op, rest = re.split(r"([$&|~\-]?=)", kf, maxsplit=1)
                f = max(0, self.parse_time(tc) + shift)
                items[f] = "%s%s%s" % (self.frame_to_tc(f), op, rest)
        prop.text = ";".join(items[f] for f in sorted(items))

    def _pad_hidden(self, flt, lo, hi):
        """For a tracker effect that covers a whole clip (first..last frame = lo..hi): make sure it is hidden from the start of
        the clip to its first box and from its last box to the end of the clip."""
        prop = next((p for p in flt.findall("property") if p.get("name") == "results"), None)
        if prop is None or not prop.text:
            return
        items = {}
        for kf in prop.text.split(";"):
            if kf:
                tc, rest = re.split(r"[$&|~\-]?=", kf, maxsplit=1)
                items[self.parse_time(tc)] = (kf, rest.strip() != "0 0 0 0")
        visible = [f for f, (_, vis) in items.items() if vis]
        if not visible:
            return
        first, last = min(visible), max(visible)
        extra = []
        if first > lo:
            extra += [lo, first - 1]
        if last < hi:
            extra.append(last + 1)
        for f in extra:
            items.setdefault(f, ("%s&=0 0 0 0" % self.frame_to_tc(f), False))
        prop.text = ";".join(items[f][0] for f in sorted(items))

    def apply_merge(self, plan, existing="keep"):
        """Carries out a plan from plan_merge(). Returns a stats dict; call save() afterwards."""
        self._fid = self._max_filter_id()
        stats = dict(clips=0, pieces=0, filters=0, whole_clip_mode=0, audio_split=0, groups_rewritten=0)
        index = {(e.track.id, e.pos): e for e in self.list_entries()}
        splits = []

        def copy_of(f):
            c = copy.deepcopy(f)
            self._fid += 1
            c.set("id", "filter%d" % self._fid)
            return c

        for item in plan:
            e, overlaps, pieces = item["entry"], item["overlaps"], item["pieces"]
            stats["clips"] += 1
            if item["whole"]:
                stats["whole_clip_mode"] += 1
                if existing == "replace":
                    for f in self._tracker_filters(e.el):
                        e.el.remove(f)
                for lo, hi, flts, shift in overlaps:
                    for f in flts:
                        if _prop(f, "mlt_service") == TRACKER_SERVICE:
                            c = copy_of(f)
                            self._shift_results(c, shift)
                            self._pad_hidden(c, e.in_f, e.out_f)       # hidden everywhere on the clip except where it was tracked
                            e.el.append(c)
                            stats["filters"] += 1
                _set_prop(e.el, "kdenlive:activeeffect", "0")
                continue
            new_els = []
            for i, (s, t, cov) in enumerate(pieces):
                el = self._clone_piece(e.el, s, t, i == 0, i == len(pieces) - 1)
                if cov:
                    if existing == "replace":
                        for f in self._tracker_filters(el):
                            el.remove(f)
                    _set_prop(el, "kdenlive:activeeffect", "0")
                    for idx in sorted(cov):
                        for f in overlaps[idx][2]:
                            c = copy_of(f)
                            self._shift_results(c, overlaps[idx][3])
                            el.append(c)
                            stats["filters"] += 1
                new_els.append(el)
            self._replace(e.playlist, e.el, new_els)
            stats["pieces"] += len(pieces)
            splits.append((e, [(s, t, bool(cov)) for s, t, cov in pieces]))
        if splits:
            self._split_audio_and_groups(splits, index, stats)
        return stats

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


# ---- Track post-processing: join fragments of one object, fill holes, thin out keyframes -----------------
# A "track" is a list of (frame, x, y, w, h); track_history maps a key like "r2_person_17" -> track.
_TRACK_KEY = re.compile(r"^(?P<prefix>r\d+_)?(?P<cls>[A-Za-z]+)_(?P<id>\d+)$")


def _lerp_boxes(a, b, frame):
    """Linear interpolation of two (frame, x, y, w, h) boxes at 'frame'."""
    t = (frame - a[0]) / float(b[0] - a[0])
    return (frame,) + tuple(int(round(a[i] + t * (b[i] - a[i]))) for i in (1, 2, 3, 4))


def _link_cost(ca, cb, gap):
    """How well does track cb continue track ca after 'gap' missing frames? None = does not fit."""
    a = ca[-1]
    if gap < 0:                                     # B starts while A still has a few frames: compare at B's start
        i = bisect.bisect_right([c[0] for c in ca[-8:]], cb[0][0]) - 1
        a = ca[-8:][max(0, i)]
    b = cb[0]
    size = float(max(a[3], a[4], b[3], b[4], 1))
    acx, acy = a[1] + a[3] / 2.0, a[2] + a[4] / 2.0
    bcx, bcy = b[1] + b[3] / 2.0, b[2] + b[4] / 2.0
    dist = math.hypot(acx - bcx, acy - bcy)
    # the object may have kept moving while it was lost: also try A's recent velocity (capped to one object size)
    pred = dist
    if len(ca) >= 4 and gap > 0:
        k = min(5, len(ca) - 1)
        vx, vy = (acx - (ca[-1 - k][1] + ca[-1 - k][3] / 2.0)) / (ca[-1][0] - ca[-1 - k][0]), \
                 (acy - (ca[-1 - k][2] + ca[-1 - k][4] / 2.0)) / (ca[-1][0] - ca[-1 - k][0])
        dx, dy = vx * (gap + 1), vy * (gap + 1)
        n = math.hypot(dx, dy)
        if n > size:
            dx, dy = dx * size / n, dy * size / n
        pred = math.hypot(acx + dx - bcx, acy + dy - bcy)
    d = min(dist, pred) / size
    g = max(gap, 0)
    area_ratio = (max(a[3], 1) * max(a[4], 1)) / float(max(b[3], 1) * max(b[4], 1))
    if d > 0.5 + 0.03 * g or not (0.33 <= area_ratio <= 3.0):
        return None
    return d + 0.1 * abs(math.log(area_ratio)) + 0.01 * g


def consolidate_tracks(track_history, max_gap=20, max_overlap=3):
    """Joins fragments that belong to ONE object (the tracker often starts a new ID after a missed frame or a
    fast movement) and fills small holes by interpolation, so the blur never drops out for a moment.
    Works in place. Only tracks of the same kind (person/vehicle) from the same scan range are joined.
    Returns the number of joins."""
    if max_gap <= 0:
        return 0
    info = {}
    for key, coords in track_history.items():
        if not coords:
            continue
        coords.sort(key=lambda c: c[0])
        m = _TRACK_KEY.match(key)
        info[key] = ((m.group("prefix") or "", m.group("cls").lower()) if m else (key, ""))
    groups = {}
    for key, grp in info.items():
        groups.setdefault(grp, []).append(key)

    pairs = []
    for keys in groups.values():
        keys.sort(key=lambda k: track_history[k][0][0])
        starts = [track_history[k][0][0] for k in keys]
        for a in keys:
            ca = track_history[a]
            end_a = ca[-1][0]
            lo = bisect.bisect_left(starts, end_a + 1 - max_overlap)
            hi = bisect.bisect_right(starts, end_a + 1 + max_gap)
            for b in keys[lo:hi]:
                cb = track_history[b]
                if b == a or cb[0][0] <= ca[0][0]:
                    continue
                gap = cb[0][0] - end_a - 1
                cost = _link_cost(ca, cb, gap)
                if cost is not None:
                    pairs.append((cost, a, b))
    pairs.sort()
    succ, pred = {}, {}
    for cost, a, b in pairs:
        if a not in succ and b not in pred:
            succ[a], pred[b] = b, a

    joins = 0
    for head in [k for k in info if k not in pred]:
        if head not in succ:
            continue
        merged = list(track_history[head])
        k = head
        while k in succ:
            k = succ[k]
            nxt = track_history[k]
            merged = [c for c in merged if c[0] < nxt[0][0]]            # drop an overlap, B wins
            for f in range(merged[-1][0] + 1, nxt[0][0]):               # fill the hole
                merged.append(_lerp_boxes(merged[-1], nxt[0], f))
            joins += 1
            merged.extend(nxt)
            del track_history[k]
        track_history[head] = merged
    # fill small holes inside every track as well (frames the detector missed for a moment)
    for key, coords in track_history.items():
        out = [coords[0]] if coords else []
        for c in coords[1:]:
            gap = c[0] - out[-1][0] - 1
            if 0 < gap <= max_gap:
                out.extend(_lerp_boxes(out[-1], c, f) for f in range(out[-1][0] + 1, c[0]))
            out.append(c)
        track_history[key] = out
    return joins


def _smooth_at(kfs, f):
    """Catmull-Rom style curve through the keyframes (frame, x, y, w, h) at 'frame' f - a stand-in for the smooth
    interpolation Kdenlive/MLT applies between keyframes ('$=' type). Returns [x, y, w, h]."""
    frames = [k[0] for k in kfs]
    i = max(0, min(len(kfs) - 2, bisect.bisect_right(frames, f) - 1))
    p0, p1, p2, p3 = kfs[max(0, i - 1)], kfs[i], kfs[i + 1], kfs[min(len(kfs) - 1, i + 2)]
    span = float(p2[0] - p1[0])
    t = (f - p1[0]) / span
    t2, t3 = t * t, t * t * t
    out = []
    for c in (1, 2, 3, 4):
        m1 = (p2[c] - p1[c]) if p0 is p1 else (p2[c] - p0[c]) * span / (p2[0] - p0[0])
        m2 = (p2[c] - p1[c]) if p3 is p2 else (p3[c] - p1[c]) * span / (p3[0] - p1[0])
        out.append((2 * t3 - 3 * t2 + 1) * p1[c] + (t3 - 2 * t2 + t) * m1 + (-2 * t3 + 3 * t2) * p2[c] + (t3 - t2) * m2)
    return out


def thin_keyframes(coords, pct, max_span=60):
    """Removes keyframes that the interpolation between their neighbours reproduces within 'pct' percent of the
    object's size (at least 2 px) - checked for straight AND smooth interpolation. Steady movement then needs only
    a few keyframes. pct <= 0 keeps every keyframe."""
    n = len(coords)
    if pct <= 0 or n <= 2:
        return list(coords)

    def tol(k):
        return max(2.0, pct / 100.0 * max(coords[k][3], coords[k][4]))

    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:                                   # 1) straight-line simplification (Douglas-Peucker in time)
        i, j = stack.pop()
        if j - i < 2:
            continue
        fi, fj = coords[i][0], coords[j][0]
        worst, worst_k = 0.0, None
        for k in range(i + 1, j):
            t = (coords[k][0] - fi) / float(fj - fi)
            err = max(abs(coords[k][c] - (coords[i][c] + t * (coords[j][c] - coords[i][c]))) for c in (1, 2, 3, 4)) / tol(k)
            if err > worst:
                worst, worst_k = err, k
        if worst > 1.0:
            split = worst_k
        elif fj - fi > max_span:
            split = min(range(i + 1, j), key=lambda k: abs(coords[k][0] - (fi + fj) / 2.0))
        else:
            continue
        keep[split] = True
        stack.append((i, split))
        stack.append((split, j))

    for _ in range(n):                             # 2) make sure the SMOOTH curve through the kept points is close too
        kept = [coords[k] for k in range(n) if keep[k]]
        worst, worst_k = 1.0, None
        for k in range(n):
            if keep[k]:
                continue
            sm = _smooth_at(kept, coords[k][0])
            err = max(abs(coords[k][c + 1] - sm[c]) for c in range(4)) / tol(k)
            if err > worst:
                worst, worst_k = err, k
        if worst_k is None:
            break
        keep[worst_k] = True
    return [coords[k] for k in range(n) if keep[k]]


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
                           consolidate_tracks=consolidate_tracks, thin_keyframes=thin_keyframes,
                           parse_user_time=parse_user_time, format_clock=format_clock)
# ======================================================================================

MERGE_BY_TIME = "by timeline time"
MERGE_PAIR = "pair video files (advanced)"
MERGE_MODES = (MERGE_BY_TIME, MERGE_PAIR)             # the first one is the default
MERGE_AUTO = "(match by file name)"
MERGE_ALL_VIDEOS = "(all videos)"
MERGE_ALL_TRACKS = "all video tracks"


def compute_window_size(screen_w, screen_h, saved=None):
    """Window size that fits the screen -> (width, height, min_width, min_height).
    Leaves room for the title bar and the taskbar, so it also works on 1280x720 screens.
    'saved' is the size the user had last time (kept if it still fits this screen)."""
    avail_w = max(640, screen_w - 20)
    avail_h = max(480, int(screen_h * 0.88))
    if saved and saved[0] > 0 and saved[1] > 0:
        w, h = min(int(saved[0]), screen_w), min(int(saved[1]), max(avail_h, screen_h - 60))
    else:
        w, h = min(1100, avail_w), min(860, avail_h)
    min_w, min_h = min(780, avail_w), min(520, avail_h)
    return max(w, min_w), max(h, min_h), min_w, min_h


class ScrollableFrame(ttk.Frame):
    """A frame whose content scrolls vertically (scrollbar + mouse wheel) if the window is too small for it."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):      # Windows/macOS, Linux up, Linux down
            self.canvas.bind_all(seq, self._on_wheel, add="+")

    def _on_wheel(self, event):
        if not self.winfo_viewable() or self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        num = getattr(event, "num", None)
        step = -1 if num == 4 else 1 if num == 5 else (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step * 3, "units")


class BlurApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.saved_window_size = None        # from the settings file; the window then opens in the size you left it

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
        self.join_gap_var = tk.IntVar(value=20)       # join fragments of one object if the gap is at most this many frames (0 = off)
        self.thin_pct_var = tk.DoubleVar(value=4.0)   # keyframe thinning tolerance in % of the object size (0 = every frame)
        self.media_dir_var = tk.StringVar(value="")        # optional: where the project's source videos live
        self.existing_var = tk.StringVar(value="keep")     # timeline mode: clips that already have a tracker effect
        self.merge_src_var = tk.StringVar(value="")        # merge tab: project that has the effects and cuts
        self.merge_dst_var = tk.StringVar(value="")        # merge tab: project that receives them
        self.merge_out_var = tk.StringVar(value="")
        self.merge_scope_var = tk.StringVar(value="blur")  # "blur" = blur effects only, "all" = other simple effects too
        self.merge_existing_var = tk.StringVar(value="keep")
        self.merge_mode_var = tk.StringVar(value=MERGE_BY_TIME)       # map by timeline time (default) or pair two video files
        self.merge_srcvid_var = tk.StringVar(value=MERGE_ALL_VIDEOS)  # which video of the source project to take the effects from
        self.merge_dstvid_var = tk.StringVar(value=MERGE_AUTO)       # which video of the target receives them
        self.merge_track_var = tk.StringVar(value=MERGE_ALL_TRACKS)  # which target track (time mode)
        self.merge_offset_var = tk.StringVar(value="0")
        self.merge_restore_var = tk.StringVar(value="auto")      # cuts inside target files: auto / undo / keep
        self.merge_samefiles_var = tk.IntVar(value=1)            # copy exactly by file where the same video is in both projects
        self._merge_src_map, self._merge_dst_map, self._merge_track_map = {}, {}, {}
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
        self.preview_src = None              # last preview frame (small PIL image), redrawn when the window is resized

        # Load saved settings on startup
        self.load_settings_from_disk(CONFIG_FILE_PATH, silent=True)

        self.create_widgets()
        self.setup_window()

    def setup_window(self):
        """Size the window to the screen (works down to 1280x720), resizable, remembers the size you choose."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h, min_w, min_h = compute_window_size(sw, sh, self.saved_window_size)
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 4)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.minsize(min_w, min_h)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def current_window_size(self):
        try:
            if self.root.winfo_viewable():
                return [self.root.winfo_width(), self.root.winfo_height()]
        except Exception:
            pass
        return list(self.saved_window_size) if self.saved_window_size else None

    def on_close(self):
        if self.running and not messagebox.askyesno(
                "Quit", "Processing is still running.\nQuit without saving the results so far?\n\n"
                        "(Use 'Cancel & Save Progress' to keep them.)"):
            return
        self.save_current_settings(silent=True)
        self.cancel_event.set()
        self.root.destroy()

    def create_widgets(self):
        # the status line is packed first (at the bottom) so it is never pushed out of a small window
        tk.Label(self.root, textvariable=self.status_var, font=("Arial", 9, "italic"), fg="gray", anchor="w").pack(side="bottom", fill="x", padx=15, pady=2)

        # --- Main Notebook (Tabs) ---
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(6, 2))

        tab_main = ttk.Frame(notebook)
        tab_config = ttk.Frame(notebook)
        
        notebook.add(tab_main, text=" Processing & Execution ")
        notebook.add(tab_config, text=" Advanced Configuration ")
        tab_merge = ttk.Frame(notebook)
        notebook.add(tab_merge, text=" Merge Effects ")
        self.build_merge_tab(tab_merge)

        # ================= TAB 1: MAIN PROCESSING =================
        file_frame = tk.LabelFrame(tab_main, text=" Files ", font=("Arial", 10, "bold"), padx=10, pady=4)
        file_frame.pack(fill="x", padx=8, pady=(6, 3))
        file_frame.columnconfigure(1, weight=1)          # the path fields stretch with the window

        self.input_label = tk.Label(file_frame, text="Kdenlive Project:")
        self.input_label.grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(file_frame, textvariable=self.input_path, width=20).grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        tk.Button(file_frame, text="Browse...", command=self.browse_input).grid(row=0, column=2, pady=2)

        tk.Label(file_frame, text="Output Target:").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(file_frame, textvariable=self.output_path, width=20).grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        tk.Button(file_frame, text="Save As...", command=self.browse_output).grid(row=1, column=2, pady=2)

        media_label = tk.Label(file_frame, text="Media Folder (optional):")
        media_label.grid(row=2, column=0, sticky="w", pady=2)
        media_entry = tk.Entry(file_frame, textvariable=self.media_dir_var, width=20)
        media_entry.grid(row=2, column=1, sticky="ew", padx=5, pady=2)
        media_button = tk.Button(file_frame, text="Browse...", command=self.browse_media_dir)
        media_button.grid(row=2, column=2, pady=2)
        self.media_widgets = [media_label, media_entry, media_button]   # only used in project mode

        # Time range and quick options share one compact frame
        self.opt_frame = tk.LabelFrame(tab_main, text=" Range & Options ", font=("Arial", 10, "bold"), padx=10, pady=4)
        self.opt_frame.pack(fill="x", padx=8, pady=3)

        tk.Label(self.opt_frame, text="Start (empty = beginning):").grid(row=0, column=0, sticky="w", pady=2)
        start_entry = tk.Entry(self.opt_frame, textvariable=self.start_time_var, width=11)
        start_entry.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        tk.Label(self.opt_frame, text="End (empty = end):").grid(row=0, column=2, sticky="w", pady=2, padx=(15, 0))
        end_entry = tk.Entry(self.opt_frame, textvariable=self.end_time_var, width=11)
        end_entry.grid(row=0, column=3, sticky="w", padx=5, pady=2)
        self.time_entries = [start_entry, end_entry]
        self.time_hint = tk.Label(self.opt_frame, text="", fg="gray")
        self.time_hint.grid(row=0, column=4, sticky="w", padx=(10, 0))

        tk.Label(self.opt_frame, text="Mode:").grid(row=1, column=0, sticky="w", pady=2)
        self.export_combo = ttk.Combobox(self.opt_frame, textvariable=self.export_mode_var, values=["kdenlive", "mkv"], state="readonly", width=9)
        self.export_combo.grid(row=1, column=1, sticky="w", padx=5, pady=2)
        self.export_combo.bind("<<ComboboxSelected>>", self.on_export_mode_changed)
        tk.Label(self.opt_frame, text="Detect:").grid(row=1, column=2, sticky="w", pady=2, padx=(15, 0))
        ttk.Combobox(self.opt_frame, textvariable=self.mode_var, values=["both", "vehicles", "persons"], state="readonly", width=9).grid(row=1, column=3, sticky="w", padx=5, pady=2)
        tk.Label(self.opt_frame, text="Preview every (s, 0 = off):").grid(row=1, column=4, sticky="w", pady=2, padx=(10, 0))
        tk.Entry(self.opt_frame, textvariable=self.preview_sec_var, width=6).grid(row=1, column=5, sticky="w", padx=5, pady=2)

        tk.Label(self.opt_frame, text="Mode kdenlive: edit a project - mode mkv: render a check video from a video file. "
                                      "Preview interval: decimals allowed, e.g. 0.5.", fg="gray", font=("Arial", 8)
                 ).grid(row=2, column=0, columnspan=6, sticky="w")

        self.run_btn = tk.Button(tab_main, text="Start Processing & Generation", bg="#4CAF50", fg="white", font=("Arial", 11, "bold"), command=self.on_run_button)
        self.run_btn.pack(fill="x", padx=8, pady=5)

        # counts and time remaining in one line below the preview (packed before the preview so they keep their space)
        info_frame = tk.Frame(tab_main)
        info_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        tk.Label(info_frame, textvariable=self.counts_var, font=("Arial", 9, "bold")).pack(side="left")
        tk.Label(info_frame, textvariable=self.eta_var, font=("Arial", 9, "bold", "italic"), fg="#1976D2").pack(side="right")

        # Live preview: takes all remaining space, the picture always fits (16:9 or whatever the video is)
        preview_frame = tk.LabelFrame(tab_main, text=" Live Preview ", font=("Arial", 10, "bold"), padx=6, pady=3)
        preview_frame.pack(fill="both", expand=True, padx=8, pady=3)

        self.preview_canvas = tk.Canvas(preview_frame, width=320, height=180, bg="black", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_text_id = self.preview_canvas.create_text(
            160, 90, text="The preview appears here when processing starts", fill="white", font=("Arial", 11)
        )
        self.preview_canvas.bind("<Configure>", self.on_preview_resize)


        # ================= TAB 2: ADVANCED CONFIGURATION =================
        cfg_scroll = ScrollableFrame(tab_config)        # scrolls if the window is too small to show everything
        cfg_scroll.pack(fill="both", expand=True)
        cfg_inner = tk.Frame(cfg_scroll.inner, padx=15, pady=10)
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

        tk.Label(inf_frame, text="Join Track Fragments (max gap, frames):").grid(row=9, column=0, sticky="w", pady=4)
        tk.Spinbox(inf_frame, from_=0, to=60, increment=5, textvariable=self.join_gap_var, width=15).grid(row=9, column=1, sticky="w", padx=10, pady=4)
        tk.Label(inf_frame, text="one person/vehicle that the tracker split into several tracks is joined; 0 = off", fg="gray").grid(row=9, column=2, sticky="w")

        tk.Label(inf_frame, text="Keyframe Thinning (% of object size):").grid(row=10, column=0, sticky="w", pady=4)
        tk.Spinbox(inf_frame, from_=0, to=15, increment=1, textvariable=self.thin_pct_var, width=15).grid(row=10, column=1, sticky="w", padx=10, pady=4)
        tk.Label(inf_frame, text="fewer keyframes = easier to edit in Kdenlive; 0 = one keyframe per frame", fg="gray").grid(row=10, column=2, sticky="w")

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
        """Helper to resolve the directory of the current path for file dialogs (works for a file name that does not exist yet)."""
        if current_path:
            d = current_path if os.path.isdir(current_path) else os.path.dirname(current_path)
            if d and os.path.isdir(d):
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
        self.time_hint.config(text="timeline time, HH:MM:SS" if project else "video time, HH:MM:SS")
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

    # ---------------------------------------------------------------- "Merge Effects" tab
    def build_merge_tab(self, tab):
        tk.Label(tab, justify="left", anchor="w", wraplength=900, fg="gray",
                 text="Copies the blur effects - and the cuts they need - from one Kdenlive project into another one. "
                      "The effects belong to the TIME in the source video, so the target project may use completely "
                      "different video files and tracks. Nothing is removed from the target, and neither project "
                      "file is overwritten."
                 ).pack(fill="x", padx=12, pady=(6, 2))

        files = tk.LabelFrame(tab, text=" Projects ", font=("Arial", 10, "bold"), padx=10, pady=3)
        files.pack(fill="x", padx=10, pady=2)
        files.columnconfigure(1, weight=1)
        rows = (("Source (has the effects):", self.merge_src_var, self.browse_merge_src, "Browse..."),
                ("Target (receives them):", self.merge_dst_var, self.browse_merge_dst, "Browse..."),
                ("Output:", self.merge_out_var, self.browse_merge_out, "Save As..."))
        for r, (label, var, cmd, btn) in enumerate(rows):
            tk.Label(files, text=label).grid(row=r, column=0, sticky="w", pady=1)
            tk.Entry(files, textvariable=var, width=20).grid(row=r, column=1, sticky="ew", padx=5, pady=1)
            tk.Button(files, text=btn, command=cmd).grid(row=r, column=2, pady=1)

        opts = tk.LabelFrame(tab, text=" How to apply ", font=("Arial", 10, "bold"), padx=10, pady=3)
        opts.pack(fill="x", padx=10, pady=2)
        opts.columnconfigure(2, weight=1)

        tk.Label(opts, text="Method:").grid(row=0, column=0, sticky="w", pady=1)
        mode_combo = ttk.Combobox(opts, textvariable=self.merge_mode_var, values=list(MERGE_MODES), state="readonly", width=24)
        mode_combo.grid(row=0, column=1, sticky="w", padx=5)
        mode_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_merge_mode_ui())
        self.merge_mode_hint = tk.Label(opts, text="", fg="gray", anchor="w", justify="left", wraplength=560)
        self.merge_mode_hint.grid(row=0, column=2, sticky="w", padx=8)

        tk.Label(opts, text="Source video:").grid(row=1, column=0, sticky="w", pady=1)
        self.merge_srcvid_combo = ttk.Combobox(opts, textvariable=self.merge_srcvid_var, values=[], state="readonly", width=40)
        self.merge_srcvid_combo.grid(row=1, column=1, columnspan=2, sticky="w", padx=5)

        tk.Label(opts, text="Target video:").grid(row=2, column=0, sticky="w", pady=1)
        self.merge_dstvid_combo = ttk.Combobox(opts, textvariable=self.merge_dstvid_var, values=[MERGE_AUTO], state="readonly", width=40)
        self.merge_dstvid_combo.grid(row=2, column=1, columnspan=2, sticky="w", padx=5)

        tk.Label(opts, text="Target track:").grid(row=3, column=0, sticky="w", pady=1)
        self.merge_track_combo = ttk.Combobox(opts, textvariable=self.merge_track_var, values=[MERGE_ALL_TRACKS], state="readonly", width=40)
        self.merge_track_combo.grid(row=3, column=1, columnspan=2, sticky="w", padx=5)

        self.merge_offset_label = tk.Label(opts, text="Offset:")
        self.merge_offset_label.grid(row=4, column=0, sticky="w", pady=1)
        tk.Entry(opts, textvariable=self.merge_offset_var, width=12).grid(row=4, column=1, sticky="w", padx=5)
        self.merge_offset_hint = tk.Label(opts, text="", fg="gray", anchor="w", justify="left", wraplength=560)
        self.merge_offset_hint.grid(row=4, column=2, sticky="w", padx=8)

        tk.Label(opts, text="Cuts in target files:").grid(row=5, column=0, sticky="w", pady=1)
        self.merge_restore_combo = ttk.Combobox(opts, textvariable=self.merge_restore_var, values=["auto", "undo", "keep"], state="readonly", width=8)
        self.merge_restore_combo.grid(row=5, column=1, sticky="w", padx=5)
        tk.Label(opts, text="undo = the source video was rendered BEFORE footage was cut out of the target's files; "
                            "auto compares the video's length with the target", fg="gray", anchor="w", justify="left", wraplength=560
                 ).grid(row=5, column=2, sticky="w", padx=8)

        self.merge_samefiles_check = tk.Checkbutton(
            opts, variable=self.merge_samefiles_var, anchor="w", justify="left",
            text="Copy exactly by file where the same video file is in both projects (recommended)")
        self.merge_samefiles_check.grid(row=6, column=0, columnspan=3, sticky="w", pady=1)

        tk.Label(opts, text="Copy:").grid(row=7, column=0, sticky="w", pady=1)
        scope = ttk.Combobox(opts, textvariable=self.merge_scope_var, values=["blur", "all"], state="readonly", width=8)
        scope.grid(row=7, column=1, sticky="w", padx=5)
        scope.bind("<<ComboboxSelected>>", lambda e: self.refresh_merge_info())
        tk.Label(opts, text="blur = blur effects only, all = other simple effects (colour, ...) as well", fg="gray").grid(row=7, column=2, sticky="w", padx=8)
        tk.Label(opts, text="Blur already in target:").grid(row=8, column=0, sticky="w", pady=1)
        ttk.Combobox(opts, textvariable=self.merge_existing_var, values=["keep", "replace"], state="readonly", width=8).grid(row=8, column=1, sticky="w", padx=5)
        tk.Label(opts, text="keep = such clips stay as they are, replace = overwritten where effects are copied", fg="gray").grid(row=8, column=2, sticky="w", padx=8)

        btns = tk.Frame(tab)
        btns.pack(fill="x", padx=10, pady=4)
        tk.Button(btns, text="Preview", command=lambda: self.run_merge(True), width=14).pack(side="left")
        tk.Button(btns, text="Merge && Save", bg="#4CAF50", fg="white", font=("Arial", 10, "bold"),
                  command=lambda: self.run_merge(False)).pack(side="left", padx=8, fill="x", expand=True)

        log = tk.Frame(tab)
        log.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.merge_text = tk.Text(log, height=4, wrap="word", state="disabled")
        bar = ttk.Scrollbar(log, orient="vertical", command=self.merge_text.yview)
        self.merge_text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.merge_text.pack(side="left", fill="both", expand=True)
        self.apply_merge_mode_ui()
        self.refresh_merge_info()                    # remembered projects from the last session fill the drop-downs again

    def apply_merge_mode_ui(self):
        """Enable only the fields that matter for the chosen method and explain them."""
        by_time = self.merge_mode_var.get() == MERGE_BY_TIME
        self.merge_dstvid_combo.config(state="disabled" if by_time else "readonly")
        self.merge_track_combo.config(state="readonly" if by_time else "disabled")
        self.merge_restore_combo.config(state="readonly" if by_time else "disabled")
        self.merge_samefiles_check.config(state="normal" if by_time else "disabled")
        if by_time:
            self.merge_mode_hint.config(text="The effects of ALL videos/tracks of the source are placed on the target's timeline "
                                             "and go onto whatever clips are there. Stops where the target has no clips any more.")
            self.merge_offset_label.config(text="Starts at:")
            self.merge_offset_hint.config(text="position on the target timeline where time 0 of the source video sits (HH:MM:SS)")
        else:
            self.merge_mode_hint.config(text="One source video is paired with a video file of the target (automatically by name, or "
                                             "chosen by hand). Stops where the target video ends.")
            self.merge_offset_label.config(text="Offset:")
            self.merge_offset_hint.config(text="target video time = source video time + offset (HH:MM:SS or seconds, may be negative, e.g. -12.5)")

    def refresh_merge_info(self):
        """Fills the drop-downs from the two chosen projects."""
        try:
            src, dst = self.merge_src_var.get(), self.merge_dst_var.get()
            if src and os.path.exists(src):
                media = KdenliveProject(src).media_with_effects(self.merge_scope_var.get())
                self._merge_src_map = {MERGE_ALL_VIDEOS: None}
                self._merge_src_map.update({"%s  (%d effects)" % (m[1], m[2]): m[0] for m in media})
                self.merge_srcvid_combo.config(values=list(self._merge_src_map))
                if self.merge_srcvid_var.get() not in self._merge_src_map:
                    self.merge_srcvid_var.set(MERGE_ALL_VIDEOS)
            if dst and os.path.exists(dst):
                target = KdenliveProject(dst)
                self._merge_dst_map = {"%s  (%d clips)" % (n, c): k for k, n, c in target.video_media()}
                self.merge_dstvid_combo.config(values=[MERGE_AUTO] + list(self._merge_dst_map))
                if self.merge_dstvid_var.get() not in [MERGE_AUTO] + list(self._merge_dst_map):
                    self.merge_dstvid_var.set(MERGE_AUTO)
                self._merge_track_map = {"track %d  (%d clips)" % (tid, n): tid for tid, n in target.video_tracks()}
                self.merge_track_combo.config(values=[MERGE_ALL_TRACKS] + list(self._merge_track_map))
                if self.merge_track_var.get() not in [MERGE_ALL_TRACKS] + list(self._merge_track_map):
                    self.merge_track_var.set(MERGE_ALL_TRACKS)
        except Exception as e:
            self.merge_show("Could not read a project: %s" % e)

    def merge_show(self, text):
        self.merge_text.config(state="normal")
        self.merge_text.delete("1.0", "end")
        self.merge_text.insert("end", text)
        self.merge_text.config(state="disabled")

    def _suggest_merge_output(self):
        dst = self.merge_dst_var.get()
        if not dst:
            return
        base, _ = os.path.splitext(dst)
        m = re.search(r"_merged(\d*)$", base)
        base = base[:m.start()] + "_merged" + str(int(m.group(1) or 1) + 1) if m else base + "_merged"
        self.merge_out_var.set(base + ".kdenlive")

    def browse_merge_src(self):
        f = filedialog.askopenfilename(title="Project with the effects and cuts", initialdir=self.get_initial_dir(self.merge_src_var.get() or self.merge_dst_var.get() or self.merge_out_var.get()),
                                       filetypes=[("Kdenlive Project", "*.kdenlive"), ("All Files", "*.*")])
        if f:
            self.merge_src_var.set(f)
            self.refresh_merge_info()
            self.save_current_settings(silent=True)

    def browse_merge_dst(self):
        f = filedialog.askopenfilename(title="Project that should receive the effects", initialdir=self.get_initial_dir(self.merge_dst_var.get() or self.merge_src_var.get() or self.merge_out_var.get()),
                                       filetypes=[("Kdenlive Project", "*.kdenlive"), ("All Files", "*.*")])
        if f:
            self.merge_dst_var.set(f)
            self._suggest_merge_output()
            self.refresh_merge_info()
            self.save_current_settings(silent=True)

    def browse_merge_out(self):
        f = filedialog.asksaveasfilename(title="Save merged project as", initialdir=self.get_initial_dir(self.merge_out_var.get() or self.merge_dst_var.get() or self.merge_src_var.get()),
                                         defaultextension=".kdenlive", filetypes=[("Kdenlive Project", "*.kdenlive")])
        if f:
            self.merge_out_var.set(f)
            self.save_current_settings(silent=True)

    @staticmethod
    def _parse_offset(text, fps):
        """'0', '00:23:41', '12.5' or '-12.5' (seconds) -> frames at 'fps'."""
        t = (text or "").strip().replace(",", ".")
        negative = t.startswith("-")
        t = t.lstrip("+-").strip()
        if not t:
            return 0
        frames = parse_user_time(t, fps)
        if frames is None:
            raise ValueError("Cannot read the offset '%s'. Use HH:MM:SS or seconds, e.g. 00:23:41 or -12.5." % text)
        return -frames if negative else frames

    def run_merge(self, dry_run):
        try:
            src, dst, out = self.merge_src_var.get(), self.merge_dst_var.get(), self.merge_out_var.get()
            if not src or not dst:
                messagebox.showerror("Error", "Please choose the source project and the target project.")
                return
            if os.path.abspath(src) == os.path.abspath(dst):
                messagebox.showerror("Error", "Source and target are the same file.")
                return
            if not dry_run:
                if not out:
                    messagebox.showerror("Error", "Please choose where to save the merged project.")
                    return
                if os.path.abspath(out) in (os.path.abspath(src), os.path.abspath(dst)):
                    messagebox.showerror("Error", "The output must be a new file, not one of the two projects.")
                    return
                if os.path.exists(out) and not messagebox.askyesno("Overwrite?", f"{os.path.basename(out)} already exists.\nOverwrite it?"):
                    return
            self.merge_show("Reading projects ...")
            self.root.update_idletasks()
            self.refresh_merge_info()
            target, source = KdenliveProject(dst), KdenliveProject(src)
            by_time = self.merge_mode_var.get() == MERGE_BY_TIME
            src_key = self._merge_src_map.get(self.merge_srcvid_var.get())          # None = all videos of the source
            dst_key = None if by_time else self._merge_dst_map.get(self.merge_dstvid_var.get())
            if not by_time and dst_key is not None and src_key is None:
                # pairing by hand needs ONE source video: take the one with the most effects
                src_key = next((v for v in self._merge_src_map.values() if v is not None), None)
            offset = self._parse_offset(self.merge_offset_var.get(), target.fps)
            existing = self.merge_existing_var.get()
            plan, report = target.plan_merge(
                source, self.merge_scope_var.get(), existing, mode="time" if by_time else "pair", src_media=src_key,
                dst_media=dst_key,
                offset=offset, track=self._merge_track_map.get(self.merge_track_var.get()) if by_time else None,
                restore_cuts={"undo": "yes", "keep": "no"}.get(self.merge_restore_var.get(), "auto"),
                same_files=bool(self.merge_samefiles_var.get()))
            text = "\n".join(report)
            if dry_run:
                text += "\n\nThis was only a preview - nothing has been written."
            elif not plan:
                text += "\n\nNothing to merge - nothing was written."
            else:
                stats = target.apply_merge(plan, existing)
                target.save(out)
                text += ("\n\nDone. %d clip(s) changed, %d effect copies added, %d audio clip(s) cut to match.\nSaved to: %s"
                         % (stats["clips"], stats["filters"], stats["audio_split"], out))
                if target.warnings:
                    text += "\n\nWarnings:\n" + "\n".join(sorted(set(target.warnings))[:8])
            self.merge_show(text)
            self.save_current_settings(silent=True)              # remember the files and options for next time
        except ValueError as e:
            self.merge_show("")
            messagebox.showerror("Error", str(e))
        except Exception as e:
            traceback.print_exc()
            self.merge_show("")
            messagebox.showerror("Error", describe_error(e))

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
            "join_gap": self.join_gap_var.get(),
            "thin_pct": self.thin_pct_var.get(),
            "media_dir": self.media_dir_var.get(),
            "existing": self.existing_var.get(),
            "preview_sec": self.preview_sec_var.get(),
            "person_min_h": self.person_min_h_var.get(),
            "person_min_area": self.person_min_area_var.get(),
            "veh_min_h": self.veh_min_h_var.get(),
            "veh_min_area": self.veh_min_area_var.get(),
            "input_path": self.input_path.get(),
            "output_path": self.output_path.get(),
            "window_size": self.current_window_size(),
            "merge_src": self.merge_src_var.get(),
            "merge_dst": self.merge_dst_var.get(),
            "merge_out": self.merge_out_var.get(),
            "merge_mode": self.merge_mode_var.get(),
            "merge_scope": self.merge_scope_var.get(),
            "merge_existing": self.merge_existing_var.get(),
            "merge_restore": self.merge_restore_var.get(),
            "merge_samefiles": self.merge_samefiles_var.get(),
            "merge_offset": self.merge_offset_var.get()
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
        if "join_gap" in d: self.join_gap_var.set(d["join_gap"])
        if "thin_pct" in d: self.thin_pct_var.set(d["thin_pct"])
        if "media_dir" in d: self.media_dir_var.set(d["media_dir"])
        if "existing" in d: self.existing_var.set(d["existing"])
        if "preview_sec" in d: self.preview_sec_var.set(str(d["preview_sec"]))
        if "person_min_h" in d: self.person_min_h_var.set(d["person_min_h"])
        if "person_min_area" in d: self.person_min_area_var.set(d["person_min_area"])
        if "veh_min_h" in d: self.veh_min_h_var.set(d["veh_min_h"])
        if "veh_min_area" in d: self.veh_min_area_var.set(d["veh_min_area"])
        if "input_path" in d: self.input_path.set(d["input_path"])
        if "output_path" in d: self.output_path.set(d["output_path"])
        for key, var in (("merge_src", self.merge_src_var), ("merge_dst", self.merge_dst_var), ("merge_out", self.merge_out_var),
                         ("merge_scope", self.merge_scope_var), ("merge_existing", self.merge_existing_var),
                         ("merge_restore", self.merge_restore_var), ("merge_offset", self.merge_offset_var),
                         ("merge_samefiles", self.merge_samefiles_var)):
            if key in d:
                var.set(d[key])
        if d.get("merge_mode") in MERGE_MODES:
            self.merge_mode_var.set(d["merge_mode"])
        elif str(d.get("merge_mode", "")).startswith("pair"):        # name used by an earlier version
            self.merge_mode_var.set(MERGE_PAIR)
        ws = d.get("window_size")
        if isinstance(ws, (list, tuple)) and len(ws) == 2:
            self.saved_window_size = (ws[0], ws[1])      # used at the next start only

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

        # weights and the OpenVINO export always go to one fixed, writable folder - never into the
        # current working directory (which can be the install folder or the home directory)
        os.makedirs(MODELS_DIR, exist_ok=True)
        weights = os.path.join(MODELS_DIR, chosen_model)
        if not os.path.exists(weights):
            self.status_var.set(f"Status: Downloading {chosen_model} (first use only, needs internet)...")

        if device_choice.startswith("intel:"):
            base_name, _ = os.path.splitext(weights)
            ov_dir = f"{base_name}_openvino_model"
            if not os.path.exists(ov_dir):
                self.status_var.set(f"Status: Exporting {chosen_model} to OpenVINO format for Intel hardware (first use only)...")
                temp_model = YOLO(weights)
                temp_model.export(format="openvino", half=True)
            return YOLO(ov_dir, task="detect")
        else:
            return YOLO(weights, task="detect")

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
        img_pil.thumbnail((1280, 720))                    # enough for any preview size, cheap to redraw when resized
        self.preview_src = img_pil
        self.draw_preview()

    def draw_preview(self):
        """Draws the last preview frame scaled to fit the canvas (keeps the aspect ratio)."""
        if self.preview_src is None:
            return
        cw, ch = max(2, self.preview_canvas.winfo_width()), max(2, self.preview_canvas.winfo_height())
        img = self.preview_src.copy()
        img.thumbnail((cw, ch))
        self.photo_ref = ImageTk.PhotoImage(img)

        if self.preview_text_id:
            self.preview_canvas.delete(self.preview_text_id)
            self.preview_text_id = None

        if self.preview_image_id is None:
            self.preview_image_id = self.preview_canvas.create_image(cw // 2, ch // 2, anchor=tk.CENTER, image=self.photo_ref)
        else:
            self.preview_canvas.coords(self.preview_image_id, cw // 2, ch // 2)
            self.preview_canvas.itemconfig(self.preview_image_id, image=self.photo_ref)

    def on_preview_resize(self, event):
        if self.preview_text_id:
            self.preview_canvas.coords(self.preview_text_id, event.width // 2, event.height // 2)
        elif self.preview_src is not None:
            self.draw_preview()

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

    @staticmethod
    def _int_setting(var, default):
        try:
            return max(0, int(float(str(var.get()).replace(",", "."))))
        except Exception:
            return default

    @staticmethod
    def _float_setting(var, default):
        try:
            return max(0.0, float(str(var.get()).replace(",", ".")))
        except Exception:
            return default

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
            join_gap = self._int_setting(self.join_gap_var, 20)

            if mode == "persons":
                target_classes = [0]
            elif mode == "vehicles":
                target_classes = [1, 2, 3, 5, 7]
            else:
                target_classes = [0, 1, 2, 3, 5, 7]

            opts = ka.Options(blur_strength=self.blur_strength_var.get(), blur_type=self.blur_type_var.get(),
                              kdenlive_id=self.kdenlive_id_var.get(), existing=self.existing_var.get(),
                              thin_pct=self._float_setting(self.thin_pct_var, 4.0))

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
            if sys.platform.startswith("linux"):
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "hw_accel;vaapi"       # VAAPI exists on Linux only

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

            joins = 0
            for th in tracks_by_path.values():
                joins += ka.consolidate_tracks(th, join_gap)       # first join fragments of one object ...
                ka.extend_tails(th, tail_buffer)                   # ... then add the tail

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
            if stats.get("keyframes_dense"):
                lines.append(f"Keyframes written: {stats['keyframes']} (instead of {stats['keyframes_dense']}); track fragments joined: {joins}")
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
            traceback.print_exc()
            messagebox.showerror("Error", describe_error(e))
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
            join_gap = self._int_setting(self.join_gap_var, 20)
            blur_intensity = self.blur_strength_var.get()

            if mode == "persons":
                target_classes = [0]
            elif mode == "vehicles":
                target_classes = [1, 2, 3, 5, 7]
            else:
                target_classes = [0, 1, 2, 3, 5, 7]

            model = self.load_yolo_model()

            if sys.platform.startswith("linux"):
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "hw_accel;vaapi"       # VAAPI exists on Linux only
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

            # --- Join fragments of one object, then apply the tail buffer ---
            ka.consolidate_tracks(track_history, join_gap)
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
            traceback.print_exc()
            messagebox.showerror("Error", describe_error(e))
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
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=(0x08000000 if os.name == "nt" else 0))
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

    if os.name == "nt":
        try:       # own taskbar identity, so the taskbar shows our icon and not the one of python.exe
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KdenliveAutoBlurTool.App")
        except Exception:
            pass
    root = tk.Tk(className="Kdenlive-blur-gui")      # WM class used by the desktop launcher to match the icon
    try:                                              # window / taskbar icon (installed next to the script)
        _here = os.path.dirname(os.path.abspath(__file__))
        _icon_set = False
        if os.name == "nt":                           # Windows: the multi-size .ico looks much sharper than the PNG
            for _dir in (_here, os.path.join(_here, "assets")):
                for _name in ("autoblur-gui.ico", APP_ID + ".ico"):
                    if not _icon_set and os.path.exists(os.path.join(_dir, _name)):
                        root.iconbitmap(default=os.path.join(_dir, _name))
                        _icon_set = True
        for _name in ("autoblur-gui.png", APP_ID + ".png"):          # Linux/macOS (and fallback): first one found
            if not _icon_set and os.path.exists(os.path.join(_here, _name)):
                root.iconphoto(True, tk.PhotoImage(file=os.path.join(_here, _name)))
                _icon_set = True
    except Exception:
        pass
    app = BlurApp(root)
    root.mainloop()