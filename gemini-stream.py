# stream_v3.py
# Sharp HD Android virtual desktop (scrcpy-server v3.1, new_display)
#  - Low-latency pipeline (frame meta, low-delay decode, latest-frame-only rendering)
#  - Higher resolution presets (1080p / 1440p / 2160p) with matching DPI
#  - Freeform "desktop mode" setup (draggable / resizable app windows)
#  - Own Recents panel (the system one is empty on a virtual display)
#  - Lock screen unlock flow, Ctrl+U, F11 fullscreen
import json
import random
import re
import shlex
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import av
from adbutils import adb
from PyQt6.QtCore import QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QPushButton, QVBoxLayout, QWidget)

SERVER_JAR = "scrcpy-server-v3.1"
SERVER_VERSION = "3.1"
REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
LOCAL_PORT = 27183
SCROLL_SIGN = 1

# ----------------------------------------------------------------------
# Resolution presets: (width, height, dpi, bitrate)
# dp width = px * 160 / dpi, so 1440p@320 gives the same UI size as 1080p@240,
# just sharper. Higher resolution costs more encode/decode time.
# ----------------------------------------------------------------------
PRESETS = {
    "1080p": (1920, 1080, 240, 16_000_000),
    "1440p": (2560, 1440, 320, 28_000_000),
    "2160p": (3840, 2160, 480, 50_000_000),
}
RESOLUTION = "1440p"
DPI_OVERRIDE = None          # e.g. 280 for a slightly larger UI than the preset
DISPLAY_W, DISPLAY_H, DISPLAY_DPI, VIDEO_BITRATE = PRESETS[RESOLUTION]
if DPI_OVERRIDE:
    DISPLAY_DPI = DPI_OVERRIDE

APPLY_WM_DENSITY = False       # fallback if new_display's DPI isn't honored
VD_SYSTEM_DECORATIONS = True   # False -> vd_system_decorations=false (verify support)

# ----------------------------------------------------------------------
# Latency / smoothness
# ----------------------------------------------------------------------
USE_FRAME_META = True          # packet boundaries from server: no parser delay
MOUSE_MOVE_INTERVAL = 0.008    # throttle drag events (~120 Hz)
# Android animation speed multiplier (1.0 = normal, 0.5 = twice as fast).
# Applied on connect and restored on exit. None = don't touch.
ANIMATION_SCALE = 0.5
ANIM_KEYS = ("window_animation_scale", "transition_animation_scale",
             "animator_duration_scale")

# ----------------------------------------------------------------------
# Desktop (freeform) mode: draggable/resizable windows with title bars.
# Applied once; some devices need ONE reboot before it takes effect.
# ----------------------------------------------------------------------
# How this script treats the phone's freeform/window settings:
# "off"        : don't touch them (use the toggles you set in Developer options
#                > Window management). Recommended.
# "session"    : turned on when you connect, restored when you disconnect
# "persistent" : stays on until you run  python stream_v3.py --restore
FREEFORM_MODE = "off"
# Launch apps opened from our Recents panel as freeform (draggable) windows
LAUNCH_FREEFORM = True

# Auto-window: when a new app opens fullscreen on the desktop, it is switched
# to a draggable / resizable window automatically (small flicker on open).
AUTO_WINDOW = True
WINDOW_FRACTION = (0.70, 0.78)   # default window size (fraction of display)
WINDOW_CASCADE = 56              # px offset between successive windows
# SystemUI can keep stale per-display state after the virtual display goes
# away (symptom: back-swipe gesture dead until reboot). Restarting it on
# exit avoids that. Only done after a freeform session.
RESTART_SYSTEMUI_AFTER_FREEFORM = True
BACKUP_PATH = Path.home() / ".stream_v3_phone_backup.json"

# Audio: "auto" (both on Android 13+, else PC only) | "pc" | "both" | "off"
#  pc   = phone audio plays on this computer only (phone goes quiet)
#  both = plays on the computer AND the phone (needs Android 13+)
# Android cannot split audio per display, so this applies to everything
# the phone plays (not calls/ringtones).
AUDIO_MODE = "auto"

LOCK_POLL_INTERVAL = 1.5                 # seconds between lock/sleep checks
DPI_STEP, DPI_MIN, DPI_MAX = 20, 160, 640  # Ctrl +/- live UI scaling
FREEFORM_SETTINGS = {
    "enable_freeform_support": "1",
    "force_resizable_activities": "1",
    "freeform_window_management": "1",
    "force_desktop_mode_on_external_displays": "1",
}

# scrcpy control message types
MSG_KEYCODE, MSG_TEXT, MSG_TOUCH, MSG_SCROLL, MSG_DISPLAY_POWER = 0, 1, 2, 3, 10

# Android MotionEvent / KeyEvent actions
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_MOUSE = -1

# Android keycodes
KEY_HOME, KEY_BACK, KEY_APP_SWITCH = 3, 4, 187

# Qt special keys -> Android keycodes
SPECIAL_KEYS = {
    Qt.Key.Key_Return: 66, Qt.Key.Key_Enter: 66,
    Qt.Key.Key_Backspace: 67, Qt.Key.Key_Delete: 112,
    Qt.Key.Key_Tab: 61, Qt.Key.Key_Escape: KEY_BACK,
    Qt.Key.Key_Up: 19, Qt.Key.Key_Down: 20,
    Qt.Key.Key_Left: 21, Qt.Key.Key_Right: 22,
    Qt.Key.Key_Home: 122, Qt.Key.Key_End: 123,
    Qt.Key.Key_PageUp: 92, Qt.Key.Key_PageDown: 93,
}

# Only used while game mode is on (F10). Otherwise W/A/S/D are typed as
# normal text so capitals and shift work.
GAME_KEYS = {Qt.Key.Key_W: 51, Qt.Key.Key_A: 29,
             Qt.Key.Key_S: 32, Qt.Key.Key_D: 30}

try:
    from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

DECODE_ERRORS = tuple(c for c in (getattr(av.error, "FFmpegError", None),
                                  av.error.InvalidDataError) if c)

# --- High-Quality QSS ---
SHARP_UI_STYLESHEET = """
QMainWindow {
    background-color: #000000;
}
QPushButton#OverlayBtn {
    background-color: rgba(20, 20, 25, 230);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 8px;
    padding: 6px 12px;
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-weight: 600;
    font-size: 11px;
}
QPushButton#OverlayBtn:hover {
    background-color: rgba(40, 40, 50, 250);
    border: 1px solid rgba(255, 255, 255, 80);
}
QPushButton#OverlayBtn:pressed {
    background-color: #0b57d0;
}
QPushButton#LockBanner {
    background-color: rgba(11, 87, 208, 245);
    color: #ffffff;
    border: none;
    border-radius: 10px;
    padding: 10px 18px;
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#LockBanner:hover {
    background-color: rgba(30, 110, 230, 255);
}
QLabel#Toast {
    background-color: rgba(20, 20, 25, 235);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 50);
    border-radius: 10px;
    padding: 10px 16px;
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-size: 12px;
}
QWidget#RecentsPanel {
    background-color: rgba(0, 0, 0, 190);
}
QLabel#RecentsTitle {
    color: #ffffff;
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-size: 20px;
    font-weight: 600;
}
QLabel#RecentsEmpty {
    color: rgba(255, 255, 255, 180);
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-size: 14px;
}
QFrame#RecentsCard {
    background-color: rgba(30, 30, 38, 245);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 12px;
}
QPushButton#CardBtn {
    background-color: transparent;
    color: #ffffff;
    border: none;
    text-align: left;
    padding: 14px 16px;
    font-family: 'Segoe UI', 'Noto Sans', 'Ubuntu', sans-serif;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#CardBtn:hover {
    background-color: rgba(11, 87, 208, 120);
    border-radius: 12px;
}
QPushButton#CardClose {
    background-color: transparent;
    color: rgba(255, 255, 255, 170);
    border: none;
    padding: 6px 12px;
    font-weight: 700;
}
QPushButton#CardClose:hover {
    color: #ffffff;
    background-color: rgba(200, 40, 40, 200);
    border-radius: 8px;
}
"""


def _read_backup():
    try:
        data = json.loads(BACKUP_PATH.read_text())
        data.setdefault("transient", {})
        data.setdefault("persistent", {})
        return data
    except (OSError, ValueError):
        return {"transient": {}, "persistent": {}}


def _write_backup(data):
    try:
        if data["transient"] or data["persistent"]:
            BACKUP_PATH.write_text(json.dumps(data))
        elif BACKUP_PATH.exists():
            BACKUP_PATH.unlink()
    except OSError:
        pass


def _apply_saved(d, saved):
    """Put phone settings back to their recorded values ('null' = unset)."""
    for k, v in saved.items():
        try:
            if v in ("", "null"):
                d.shell(f"settings delete global {k}")
            else:
                d.shell(f"settings put global {k} {v}")
        except Exception:
            pass


def restore_phone_settings():
    """CLI: python stream_v3.py --restore"""
    devices = adb.device_list()
    if not devices:
        print("No device found")
        return
    d = devices[0]
    data = _read_backup()
    saved = {**data["persistent"], **data["transient"]}
    if not saved:
        # No backup (e.g. a crash before it was written): only reset the
        # animation speeds. Freeform toggles are left alone because you may
        # have turned them on yourself in Developer options.
        saved = {k: "null" for k in ANIM_KEYS}
        print("No backup found; resetting animation speeds only")
    _apply_saved(d, saved)
    _write_backup({"transient": {}, "persistent": {}})
    print("Phone settings restored:", ", ".join(saved))


def _split_meta(buf: bytearray):
    """Split scrcpy frame-meta stream: [u64 pts/flags][u32 size][payload]."""
    out = []
    while len(buf) >= 12:
        size = struct.unpack_from(">I", buf, 8)[0]
        if len(buf) < 12 + size:
            break
        flags = struct.unpack_from(">Q", buf, 0)[0]
        payload = bytes(buf[12:12 + size])
        del buf[:12 + size]
        out.append((flags, payload))
    return out


def _extract_packets(buf: bytearray):
    return [av.Packet(p) for _, p in _split_meta(buf) if p]


class AudioPlayer:
    """Plays the phone's audio on this computer (48 kHz, 16-bit, stereo)."""

    def __init__(self):
        self.ok = False
        self.muted = False
        self.sink = None
        self.io = None
        try:
            dev = QMediaDevices.defaultAudioOutput()
            if dev.isNull():
                print("No audio output device found")
                return
            fmt = QAudioFormat()
            fmt.setSampleRate(48000)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self.sink = QAudioSink(dev, fmt)
            self.sink.setBufferSize(48000 * 4 // 8)   # ~125 ms
            self.io = self.sink.start()
            self.ok = self.io is not None
        except Exception as e:
            print(f"Audio output unavailable: {e!r}")

    def write(self, data: bytes):
        if not self.ok or self.muted:
            return
        n = min(len(data), self.sink.bytesFree()) & ~3   # whole frames only
        if n > 0:
            self.io.write(data[:n])

    def close(self):
        if self.sink:
            self.sink.stop()


class StreamWorker(QThread):
    frame_ready = pyqtSignal()
    status = pyqtSignal(str)
    pin_requested = pyqtSignal()
    recents_ready = pyqtSignal(list)
    lock_changed = pyqtSignal(bool)     # True = phone asleep/locked, desktop stalled
    audio_data = pyqtSignal(bytes)

    def __init__(self):
        super().__init__()
        self._running = True
        self._shell = None
        self.control_sock = None
        self.device = None
        self.frame_w = 0
        self.frame_h = 0
        self.display_off = False
        self.display_id = None
        self._pin = None
        self._pin_event = threading.Event()
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._latest = None
        self._frame_pending = False
        self._backup = {"transient": {}, "persistent": {}}
        self.sdk = 0
        self.audio_sock = None
        self._audio_mode = None
        self._input_cmd = "input"
        self._fail_count = 0
        self._block_until = 0.0
        self._last_frame = time.monotonic()
        self._freeform_applied = False
        self.current_dpi = DISPLAY_DPI
        self._seen_tasks = set()
        self._win_fail = 0
        self._auto_window_on = AUTO_WINDOW
        self._cascade = 0

    def stop(self):
        self._running = False
        self._pin_event.set()  # release any pending PIN wait

    # ------------------------------------------------------------------
    # Latest-frame-only hand-off (never queue stale frames)
    # ------------------------------------------------------------------
    def _publish(self, img):
        self._last_frame = time.monotonic()
        with self._frame_lock:
            self._latest = img
            if self._frame_pending:
                return
            self._frame_pending = True
        self.frame_ready.emit()

    def take_frame(self):
        with self._frame_lock:
            img, self._latest = self._latest, None
            self._frame_pending = False
        return img

    # ------------------------------------------------------------------
    # Device settings: desktop mode + animation speed
    # ------------------------------------------------------------------
    def _set_global(self, d, key, value, kind):
        """Record the original value (crash-safe, on disk) then change it.
        kind: 'transient' = restored on exit, 'persistent' = kept until --restore"""
        bucket = self._backup[kind]
        if key not in bucket:
            bucket[key] = d.shell(f"settings get global {key}").strip()
            _write_backup(self._backup)
        d.shell(f"settings put global {key} {value}")

    def _apply_desktop_settings(self, d):
        if FREEFORM_MODE in ("session", "persistent"):
            kind = "transient" if FREEFORM_MODE == "session" else "persistent"
            try:
                for k, v in FREEFORM_SETTINGS.items():
                    self._set_global(d, k, v, kind)
                self._freeform_applied = True
                self.status.emit(
                    f"Freeform desktop mode on ({FREEFORM_MODE}). Apps opened "
                    "from now on get title bars you can drag/resize; apps "
                    "already open stay fullscreen, so close and reopen them.")
            except Exception as e:
                self.status.emit(f"Freeform setup failed: {e!r}")

        if ANIMATION_SCALE is not None:
            try:
                for k in ANIM_KEYS:
                    self._set_global(d, k, ANIMATION_SCALE, "transient")
            except Exception as e:
                self.status.emit(f"Animation scale failed: {e!r}")

    def _restore_transient(self, d):
        _apply_saved(d, self._backup["transient"])
        self._backup["transient"] = {}
        _write_backup(self._backup)

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------
    def _probe_device(self, d):
        try:
            self.sdk = int(d.shell("getprop ro.build.version.sdk").strip() or 0)
        except ValueError:
            self.sdk = 0
        model = d.shell("getprop ro.product.model").strip()
        self.status.emit(f"Device: {model} (Android API {self.sdk})")
        # Target display 0 explicitly, so keystrokes reach the lock screen even
        # when the virtual display currently holds input focus.
        out = d.shell("input -d 0 keyevent 0")
        self._input_cmd = ("input" if re.search(r"sage|rror|nknown", out)
                           else "input -d 0")

    def _dump_window_settings(self, d):
        """Print window-management related settings so we can see which keys
        this phone actually uses (they differ between Android/OEM versions)."""
        pat = "freeform|resizable|desktop|multi_window|multiwindow|taskbar"
        for ns in ("global", "secure", "system"):
            try:
                out = d.shell(f"settings list {ns} | grep -iE '{pat}'").strip()
            except Exception:
                continue
            if out:
                print(f"[settings {ns}]\n{out}")

    def _audio_plan(self):
        if AUDIO_MODE == "off" or not HAVE_AUDIO or self.sdk < 30:
            return None
        mode = AUDIO_MODE
        if mode == "auto":
            mode = "both" if self.sdk >= 33 else "pc"
        if mode == "both" and self.sdk < 33:
            mode = "pc"
        return mode

    # ------------------------------------------------------------------
    # Server startup
    # ------------------------------------------------------------------
    def _start_server(self, d, scid):
        d.sync.push(SERVER_JAR, REMOTE_JAR)
        d.forward(f"tcp:{LOCAL_PORT}", f"localabstract:scrcpy_{scid:08x}")

        extra = "" if VD_SYSTEM_DECORATIONS else " vd_system_decorations=false"
        meta = "true" if USE_FRAME_META else "false"
        self._audio_mode = self._audio_plan()
        if self._audio_mode:
            audio_opts = "audio=true audio_codec=raw"
            if self._audio_mode == "both":
                audio_opts += " audio_dup=true"
        else:
            audio_opts = "audio=false"
        cmd = (
            f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server "
            f"{SERVER_VERSION} scid={scid:08x} log_level=info tunnel_forward=true "
            f"{audio_opts} control=true max_size=0 video_bit_rate={VIDEO_BITRATE} "
            f"max_fps=60 send_device_meta=false send_codec_meta=false "
            f"send_frame_meta={meta} "
            f"new_display={DISPLAY_W}x{DISPLAY_H}/{DISPLAY_DPI}"
            f"{extra}"
        )
        self._shell = d.shell(cmd, stream=True)
        conn = self._shell

        def drain():
            buf = ""
            try:
                while True:
                    chunk = conn.read(1024)
                    if not chunk:
                        break
                    buf += (chunk.decode("utf-8", "ignore")
                            if isinstance(chunk, bytes) else chunk)
                    if self.display_id is None:
                        # server logs e.g. "New display: 2560x1440/320 (id=5)"
                        m = re.search(r"\(id=(\d+)\)", buf)
                        if m:
                            self.display_id = int(m.group(1))
                    buf = buf[-512:]
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

    def _get_display_id(self, d):
        if self.display_id is not None:
            return self.display_id
        try:
            out = d.shell("dumpsys display")
            m = re.search(
                r'DisplayInfo\{"scrcpy[^"]*",\s*displayId[ =](\d+)', out)
            if m:
                self.display_id = int(m.group(1))
        except Exception:
            pass
        return self.display_id

    def _apply_density_override(self, d):
        """Fallback if new_display's DPI isn't honored.
        NEVER run `wm density` without -d: that changes the phone's real density."""
        if not APPLY_WM_DENSITY:
            return
        for _ in range(30):
            if self.display_id is not None:
                break
            time.sleep(0.1)
        did = self._get_display_id(d)
        if did is None:
            self.status.emit("Display id not found; skipping wm density")
            return
        d.shell(f"wm density {DISPLAY_DPI} -d {did}")

    def _connect_video(self):
        for _ in range(50):
            s = socket.socket()
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.connect(("127.0.0.1", LOCAL_PORT))
                if s.recv(1):
                    return s
            except OSError:
                pass
            s.close()
            time.sleep(0.1)
        raise RuntimeError("Could not connect video socket")

    def _connect_aux(self):
        s = socket.socket()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.connect(("127.0.0.1", LOCAL_PORT))
        return s

    def _audio_loop(self, sock):
        buf = bytearray()
        sock.settimeout(1.0)
        while self._running:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            for flags, payload in _split_meta(buf):
                if (flags >> 63) & 1:        # config packet, nothing to play
                    continue
                if payload:
                    self.audio_data.emit(payload)

    def _connect_control(self):
        s = socket.socket()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.connect(("127.0.0.1", LOCAL_PORT))

        def drain_control():
            try:
                while s.recv(4096):
                    pass
            except OSError:
                pass

        threading.Thread(target=drain_control, daemon=True).start()
        return s

    # ------------------------------------------------------------------
    # Lock screen handling (keyguard lives on the physical display 0,
    # so we unlock via ADB before/while streaming the virtual display)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_awake(d) -> bool:
        out = d.shell(
            "dumpsys power | grep -o 'mWakefulness=[A-Za-z]*' | head -n 1")
        # Fail safe: only report asleep when clearly Asleep/Dozing
        return not re.search(r"Asleep|Dozing", out)

    @staticmethod
    def _keyguard_lines(d) -> str:
        return d.shell(
            "dumpsys window | grep -E 'mKeyguardShowing|isKeyguardShowing|"
            "mShowingLockscreen|mDreamingLockscreen|mBouncerShowing|"
            "isBouncerShowing'")

    @classmethod
    def _is_locked(cls, d) -> bool:
        return bool(re.search(
            r"(mKeyguardShowing|isKeyguardShowing|mShowingLockscreen"
            r"|mDreamingLockscreen)=true", cls._keyguard_lines(d)))

    @staticmethod
    def _screen_size(d):
        found = re.findall(r"(\d+)x(\d+)", d.shell("wm size"))
        if found:
            w, h = found[-1]
            return int(w), int(h)
        return 1080, 2400

    def _lock_debug(self, d) -> str:
        lines = " ".join(
            ln.strip() for ln in self._keyguard_lines(d).splitlines()[:6])
        return f"awake={self._is_awake(d)} api={self.sdk} {lines}"

    def _wake(self, d):
        if not self._is_awake(d):
            d.shell(f"{self._input_cmd} keyevent KEYCODE_WAKEUP")
            for _ in range(30):
                if self._is_awake(d):
                    break
                time.sleep(0.1)
        time.sleep(0.3)  # let the keyguard finish animating in

    def provide_pin(self, pin):
        """Called from the UI thread. None = user cancelled."""
        self._pin = pin
        self._pin_event.set()

    def _unlock(self, d, secret: str) -> bool:
        inp = self._input_cmd
        self._wake(d)
        d.shell("wm dismiss-keyguard")
        time.sleep(0.7)
        if not self._is_locked(d):
            return True                    # no secure lock, or already clear
        if not secret:
            return False
        w, h = self._screen_size(d)
        # Some devices only reveal the PIN pad after a swipe up
        d.shell(f"{inp} swipe {w // 2} {int(h * 0.85)} "
                f"{w // 2} {int(h * 0.25)} 250")
        time.sleep(0.8)
        if secret.isdigit():
            # Digits -> KEYCODE_0..9 (7..16), small gaps so none are dropped
            script = "; sleep 0.07; ".join(
                f"{inp} keyevent {7 + int(c)}" for c in secret)
        else:
            script = f"{inp} text {shlex.quote(secret.replace(' ', '%s'))}"
        d.shell(script)
        time.sleep(0.35)
        if self._is_locked(d):
            d.shell(f"{inp} keyevent 66")  # ENTER, for PINs without auto-submit
            for _ in range(25):
                time.sleep(0.2)
                if not self._is_locked(d):
                    return True
            return False
        return True

    def _try_unlock(self, d, secret: str) -> bool:
        if time.monotonic() < self._block_until:
            self.status.emit("Unlock paused after repeated failures. Wait a "
                             "minute, or unlock on the phone.")
            return False
        ok = self._unlock(d, secret)
        if ok:
            self._fail_count = 0
            self.status.emit("Unlocked")
            return True
        # Android applies timed lockouts after several wrong PINs
        self._fail_count += 1
        if self._fail_count >= 3:
            self._block_until = time.monotonic() + 60
            self._fail_count = 0
        self.status.emit("Unlock failed: " + self._lock_debug(d))
        return False

    def _ensure_unlocked(self, d):
        self._wake(d)
        if self._is_locked(d):
            d.shell("wm dismiss-keyguard")   # clears swipe-only locks
            time.sleep(0.6)
        attempts = 0
        while self._is_locked(d) and self._running and attempts < 2:
            self._pin_event.clear()
            self._pin = None
            self.status.emit("Device locked")
            self.pin_requested.emit()
            self._pin_event.wait(60)
            if not self._running or not self._pin:
                self.status.emit("Unlock skipped")
                return
            secret, self._pin = self._pin, None
            attempts += 1
            if self._try_unlock(d, secret):
                return

    def unlock_now(self, secret: str):
        """Mid-session unlock (banner / Ctrl+U). Empty secret = just wake."""
        d = self.device
        if not d or secret is None:
            return

        def job():
            try:
                self._wake(d)
                if not self._is_locked(d):
                    self.status.emit("Device already unlocked")
                    return
                self._try_unlock(d, secret)
            except Exception as e:
                self.status.emit(f"Unlock error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def _watch_lock(self, d):
        """Tell the UI when the phone sleeps/locks and the desktop stalls."""
        state = None
        while self._running:
            time.sleep(LOCK_POLL_INTERVAL)
            try:
                awake = self._is_awake(d)
                locked = awake and self._is_locked(d)
                stale = (time.monotonic() - self._last_frame) > 2.0
                blocked = (not awake) or (locked and stale)
            except Exception:
                continue
            if blocked != state:
                state = blocked
                self.lock_changed.emit(blocked)

    # ------------------------------------------------------------------
    # Tasks on the virtual display: recents, switch, close, snap
    # ------------------------------------------------------------------
    @staticmethod
    def _dump_tasks(d):
        return d.shell(
            "dumpsys activity activities | "
            r"grep -E 'Display #|Task\{|ActivityRecord\{'")

    def _task_table(self, d):
        """[(task_id, 'pkg/.Activity', mode)] top-most first for the virtual
        display. mode is fullscreen / freeform / ... or '?' if unreadable.
        Parsed from `dumpsys activity activities` (best effort)."""
        did = self._get_display_id(d)
        if did is None:
            return []
        comps, modes, order = {}, {}, []
        cur_display = None
        for line in self._dump_tasks(d).splitlines():
            m = re.search(r"Display #(\d+)", line)
            if m:
                cur_display = int(m.group(1))
                continue
            if cur_display != did:
                continue
            m = re.search(r"Task\{\w+ #(\d+)\b[^}]*?\bmode=([\w-]+)", line)
            if m:
                modes[int(m.group(1))] = m.group(2)
            m = re.search(
                r"ActivityRecord\{\w+ u\d+ ([\w.$]+/[\w.$]+) t(\d+)", line)
            if m:
                tid = int(m.group(2))
                if tid not in comps:
                    comps[tid] = m.group(1)
                    order.append(tid)
        return [(t, comps[t], modes.get(t, "?")) for t in order]

    @staticmethod
    def _is_system(comp: str) -> bool:
        low = comp.lower()
        return "launcher" in low or "systemui" in low

    def _list_tasks(self, d):
        """[(task_id, component)] of real apps on the virtual display."""
        return [(t, c) for t, c, _m in self._task_table(d)
                if not self._is_system(c)]

    # --- windowing -----------------------------------------------------
    def _launch(self, d, comp, windowing=None):
        """Start/resume comp on the virtual display. windowing: 5 = freeform
        window, 1 = fullscreen. Same flags the launcher uses (resumes task)."""
        did = self._get_display_id(d)
        flag = f"--display {did} " if did is not None else ""
        extra = f"--windowingMode {windowing} " if windowing else ""
        return d.shell(
            f"am start {flag}{extra}-f 0x10200000 "
            "-a android.intent.action.MAIN "
            "-c android.intent.category.LAUNCHER "
            f"-n {shlex.quote(comp)}")

    def _bounds_for(self, n):
        W, H = DISPLAY_W, DISPLAY_H
        w, h = int(W * WINDOW_FRACTION[0]), int(H * WINDOW_FRACTION[1])
        off = (n % 6) * WINDOW_CASCADE
        left = min(max(0, (W - w) // 2 - 2 * WINDOW_CASCADE + off), W - w)
        top = min(int(H * 0.05) + off, max(0, H - h - int(H * 0.10)))
        return left, top, left + w, top + h

    def _make_windowed(self, d, tid, comp) -> bool:
        """Turn an app into a draggable/resizable window. True if the phone
        accepted it (task reports a non-fullscreen mode afterwards)."""
        out = self._launch(d, comp, 5)
        if re.search(r"Unknown option|Error:", out):
            return False
        time.sleep(0.6)
        box = self._bounds_for(self._cascade)
        self._cascade += 1
        d.shell("am task resize %d %d %d %d %d" % ((tid,) + box))  # best effort
        time.sleep(0.4)
        for t, c, mode in self._task_table(d):
            if t == tid or c == comp:
                return mode not in ("fullscreen", "?")
        return False

    def _auto_window_loop(self, d):
        """Watch for newly opened apps and switch them to windows."""
        first = True
        while self._running and self._auto_window_on:
            time.sleep(1.5)
            try:
                if self._get_display_id(d) is None:
                    continue
                table = self._task_table(d)
                if first:
                    # Apps that were already open are left as they are
                    self._seen_tasks = {t for t, _c, _m in table}
                    first = False
                    if table and all(m == "?" for _t, _c, m in table):
                        self.status.emit(
                            "Can't read window modes on this Android version; "
                            "auto-window disabled. Use the Window button.")
                        return
                    continue
                for tid, comp, mode in table:
                    if tid in self._seen_tasks:
                        continue
                    self._seen_tasks.add(tid)
                    if self._is_system(comp) or mode != "fullscreen":
                        continue
                    if self._make_windowed(d, tid, comp):
                        self._win_fail = 0
                    else:
                        self._win_fail += 1
                        if self._win_fail >= 2:
                            self._auto_window_on = False
                            self.status.emit(
                                "The phone keeps apps fullscreen on this "
                                "display. In Developer options also try "
                                "'Force desktop mode' (same section), then "
                                "reconnect, and send me the settings list "
                                "printed at startup.")
                            return
            except Exception as e:
                self.status.emit(f"Auto-window error: {e!r}")

    def request_recents(self):
        d = self.device
        if not d:
            self.recents_ready.emit([])
            return

        def job():
            try:
                self.recents_ready.emit(self._list_tasks(d))
            except Exception as e:
                self.status.emit(f"Recents error: {e!r}")
                self.recents_ready.emit([])

        threading.Thread(target=job, daemon=True).start()

    def open_task(self, comp: str):
        d = self.device
        if not d:
            return

        def job():
            try:
                if LAUNCH_FREEFORM:
                    out = self._launch(d, comp, 5)
                    if re.search(r"Unknown option|Error:", out):
                        self._launch(d, comp)
                else:
                    self._launch(d, comp)
            except Exception as e:
                self.status.emit(f"Open error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def close_app(self, comp: str):
        d = self.device
        if not d:
            return
        pkg = comp.split("/")[0]

        def job():
            try:
                d.shell(f"am force-stop {shlex.quote(pkg)}")
                time.sleep(0.4)
                self.recents_ready.emit(self._list_tasks(d))
            except Exception as e:
                self.status.emit(f"Close error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def _top_app(self, d):
        for t, c, m in self._task_table(d):
            if not self._is_system(c):
                return t, c, m
        return None

    def snap_window(self, direction: str):
        """Ctrl+Shift+Arrow: left/right half, maximize, or centered window.
        Switches the app to a window first if it is fullscreen."""
        d = self.device
        if not d:
            return
        W, H = DISPLAY_W, DISPLAY_H
        boxes = {
            "left": (0, 0, W // 2, H),
            "right": (W // 2, 0, W, H),
            "up": (0, 0, W, H),
            "down": (W // 5, H // 6, W * 4 // 5, H * 5 // 6),
        }
        box = boxes.get(direction)
        if not box:
            return

        def job():
            try:
                top = self._top_app(d)
                if not top:
                    self.status.emit("Snap: no app window found")
                    return
                tid, comp, mode = top
                if mode != "freeform" and not self._make_windowed(d, tid, comp):
                    self.status.emit("Snap: this phone wouldn't make the app "
                                     "a window")
                    return
                out = d.shell("am task resize %d %d %d %d %d" % ((tid,) + box))
                if re.search(r"rror|nknown|xception", out):
                    self.status.emit(
                        "Snap not supported by this Android version")
            except Exception as e:
                self.status.emit(f"Snap error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def toggle_window(self):
        """Window button / Ctrl+Shift+W: window <-> maximized for the top app."""
        d = self.device
        if not d:
            return

        def job():
            try:
                top = self._top_app(d)
                if not top:
                    self.status.emit("No app window found")
                    return
                tid, comp, mode = top
                if mode == "freeform":
                    self._launch(d, comp, 1)
                    self.status.emit("Maximized")
                elif self._make_windowed(d, tid, comp):
                    self.status.emit("Windowed: drag the top bar to move, "
                                     "drag edges/corners to resize")
                else:
                    self.status.emit("This phone wouldn't make the app a "
                                     "window")
            except Exception as e:
                self.status.emit(f"Window error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def close_active(self):
        """Close button: stop the top app (same as the X in Recents)."""
        d = self.device
        if not d:
            return

        def job():
            try:
                top = self._top_app(d)
                if not top:
                    self.status.emit("No app window found")
                    return
                pkg = top[1].split("/")[0]
                d.shell(f"am force-stop {shlex.quote(pkg)}")
                self.status.emit(f"Closed {pkg.split('.')[-1]}")
            except Exception as e:
                self.status.emit(f"Close error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    def adjust_density(self, delta: int):
        """Ctrl +/-/0: change the virtual display's DPI live (0 = reset).
        Scales apps AND the system bars/notifications on that display."""
        d = self.device
        if not d:
            return

        def job():
            try:
                did = self._get_display_id(d)
                if did is None:
                    self.status.emit("Display id not found; can't change DPI")
                    return
                new = DISPLAY_DPI if delta == 0 else self.current_dpi + delta
                new = max(DPI_MIN, min(DPI_MAX, new))
                out = d.shell(f"wm density {new} -d {did}")
                if re.search(r"rror|xception|nknown", out):
                    self.status.emit(f"DPI change failed: {out.strip()[:80]}")
                else:
                    self.current_dpi = new
                    self.status.emit(f"Display density: {new} dpi")
            except Exception as e:
                self.status.emit(f"DPI error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------------
    # Control messages
    # ------------------------------------------------------------------
    def _send(self, data: bytes):
        s = self.control_sock
        if not s:
            return
        with self._lock:
            try:
                s.sendall(data)
            except OSError:
                pass

    def _pos(self, nx, ny):
        w, h = self.frame_w, self.frame_h
        x = min(max(int(nx * w), 0), w - 1)
        y = min(max(int(ny * h), 0), h - 1)
        return x, y, w, h

    def send_touch(self, action, nx, ny):
        if not self.frame_w:
            return
        x, y, w, h = self._pos(nx, ny)
        pressure = 0 if action == ACTION_UP else 0xFFFF
        buttons = 0 if action == ACTION_UP else 1
        action_button = 1 if action in (ACTION_DOWN, ACTION_UP) else 0
        self._send(struct.pack(
            ">BBqiiHHHII", MSG_TOUCH, action, POINTER_ID_MOUSE,
            x, y, w, h, pressure, action_button, buttons))

    def send_scroll(self, nx, ny, vscroll):
        if not self.frame_w:
            return
        x, y, w, h = self._pos(nx, ny)
        v = max(-1.0, min(1.0, vscroll * SCROLL_SIGN))
        v16 = int(v * 0x7FFF)
        self._send(struct.pack(">BiiHHhhI", MSG_SCROLL, x, y, w, h, 0, v16, 0))

    def send_keycode(self, keycode):
        for action in (ACTION_DOWN, ACTION_UP):
            self._send(struct.pack(">BBiii", MSG_KEYCODE, action, keycode, 0, 0))

    def send_text(self, text):
        raw = text.encode("utf-8")[:300]
        self._send(struct.pack(">BI", MSG_TEXT, len(raw)) + raw)

    def set_display_power(self, on: bool):
        self.display_off = not on
        self._send(struct.pack(">BB", MSG_DISPLAY_POWER, 1 if on else 0))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    @staticmethod
    def _make_decoder():
        codec = av.CodecContext.create("h264", "r")
        try:
            codec.thread_type = "SLICE"   # frame threading adds latency
            codec.options = {"flags": "low_delay", "flags2": "fast"}
        except Exception:
            pass
        return codec

    def run(self):
        d = None
        sock = None
        try:
            devices = adb.device_list()
            if not devices:
                self.status.emit("No device found")
                return
            d = devices[0]
            self.device = d

            # A previous run that crashed may have left settings changed
            self._backup = _read_backup()
            if self._backup["transient"]:
                self._restore_transient(d)

            self._probe_device(d)
            self._dump_window_settings(d)

            # Wake + unlock the physical display first, then start scrcpy
            self._ensure_unlocked(d)
            if not self._running:
                return

            self._apply_desktop_settings(d)

            self._start_server(d, random.randint(1, 0x7FFFFFFF))
            sock = self._connect_video()
            if self._audio_mode:
                # Server accepts: video, then audio, then control
                try:
                    self.audio_sock = self._connect_aux()
                    threading.Thread(target=self._audio_loop,
                                     args=(self.audio_sock,),
                                     daemon=True).start()
                except OSError as e:
                    self.status.emit(f"Audio unavailable: {e}")
                    self.audio_sock = None
            self.control_sock = self._connect_control()
            threading.Thread(target=self._watch_lock, args=(d,),
                             daemon=True).start()
            if AUTO_WINDOW:
                threading.Thread(target=self._auto_window_loop, args=(d,),
                                 daemon=True).start()
            self._apply_density_override(d)
            sock.settimeout(1.0)
            self.status.emit(
                f"Connected ({DISPLAY_W}x{DISPLAY_H} @ {DISPLAY_DPI} dpi, "
                f"audio: {self._audio_mode or 'off'})")

            codec = self._make_decoder()
            buf = bytearray()
            while self._running:
                try:
                    data = sock.recv(262144)
                except socket.timeout:
                    continue
                except OSError as e:
                    self.status.emit(f"Socket error: {e}")
                    break
                if not data:
                    break

                if USE_FRAME_META:
                    buf += data
                    packets = _extract_packets(buf)
                else:
                    packets = codec.parse(data)

                # Decode everything (needed for reference frames) but only
                # convert/paint the newest frame of this batch.
                last = None
                for packet in packets:
                    try:
                        frames = codec.decode(packet)
                    except DECODE_ERRORS:
                        continue
                    if frames:
                        last = frames[-1]

                if last is not None:
                    arr = last.to_ndarray(format="bgra")
                    h, w, _ = arr.shape
                    self.frame_w, self.frame_h = w, h
                    img = QImage(arr.data, w, h, 4 * w,
                                 QImage.Format.Format_RGB32)
                    img._keep = arr  # keep buffer alive; no extra copy
                    self._publish(img)
        except Exception as e:
            self.status.emit(f"Error: {e!r}")
        finally:
            if self.display_off:
                self.set_display_power(True)
            for s in (sock, self.audio_sock, self.control_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            if d:
                self._restore_transient(d)
                if (self._freeform_applied and FREEFORM_MODE == "session"
                        and RESTART_SYSTEMUI_AFTER_FREEFORM):
                    try:
                        d.shell("am crash com.android.systemui")
                    except Exception:
                        pass
                try:
                    d.forward_remove(f"tcp:{LOCAL_PORT}")
                except Exception:
                    pass


class RecentsPanel(QWidget):
    chosen = pyqtSignal(str)
    closed = pyqtSignal(str)
    dismissed = pyqtSignal()

    COLUMNS = 4

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("RecentsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 64, 48, 48)
        outer.setSpacing(20)
        title = QLabel("Recent apps")
        title.setObjectName("RecentsTitle")
        outer.addWidget(title)
        self._grid = QGridLayout()
        self._grid.setSpacing(14)
        outer.addLayout(self._grid)
        outer.addStretch(1)

    @staticmethod
    def _pretty(comp):
        pkg = comp.split("/")[0]
        return pkg.split(".")[-1].capitalize(), pkg

    def _clear(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def populate(self, items):
        """items: None = loading, [] = empty, else [(task_id, component)]."""
        self._clear()
        if items is None or not items:
            text = "Loading..." if items is None else \
                "No recent apps on this display"
            lbl = QLabel(text)
            lbl.setObjectName("RecentsEmpty")
            self._grid.addWidget(lbl, 0, 0)
            return
        for i, (_tid, comp) in enumerate(items[:8]):
            name, pkg = self._pretty(comp)
            card = QFrame()
            card.setObjectName("RecentsCard")
            lay = QHBoxLayout(card)
            lay.setContentsMargins(0, 0, 4, 0)
            lay.setSpacing(0)
            btn = QPushButton(f"{name}\n{pkg}")
            btn.setObjectName("CardBtn")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setMinimumWidth(220)
            btn.clicked.connect(lambda _=False, c=comp: self.chosen.emit(c))
            x = QPushButton("X")
            x.setObjectName("CardClose")
            x.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            x.setToolTip("Close app")
            x.clicked.connect(lambda _=False, c=comp: self.closed.emit(c))
            lay.addWidget(btn, 1)
            lay.addWidget(x, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(card, i // self.COLUMNS, i % self.COLUMNS)

    def show_items(self, items):
        self.populate(items)
        self.show()
        self.raise_()

    def mousePressEvent(self, e):
        # click on the empty backdrop dismisses
        self.hide()
        self.dismissed.emit()
        e.accept()

    def wheelEvent(self, e):
        e.accept()  # don't leak scrolling to the phone


class VideoWidget(QWidget):
    touch = pyqtSignal(int, float, float)
    scroll = pyqtSignal(float, float, float)
    key = pyqtSignal(int)
    text = pyqtSignal(str)
    unlock_shortcut = pyqtSignal()
    recents_requested = pyqtSignal()
    snap_requested = pyqtSignal(str)
    density_requested = pyqtSignal(int)
    window_toggle_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.image = None
        self._pressed = False
        self._last_move = 0.0
        self.game_mode = False

        # Floating top-left toolbar
        self.toolbar = QWidget(self)
        lay = QHBoxLayout(self.toolbar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        def mk(text, width):
            b = QPushButton(text)
            b.setObjectName("OverlayBtn")
            b.setFixedSize(width, 32)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            lay.addWidget(b)
            return b

        self.screen_btn = mk("Screen Off", 100)
        self.screen_btn.setCheckable(True)
        self.back_btn = mk("Back", 60)
        self.home_btn = mk("Home", 60)
        self.recents_btn = mk("Recents", 76)
        self.mute_btn = mk("Mute", 64)
        self.mute_btn.setCheckable(True)
        self.dpi_down_btn = mk("A-", 40)
        self.dpi_up_btn = mk("A+", 40)
        self.window_btn = mk("Window", 72)
        self.close_btn = mk("Close", 60)
        self.back_btn.clicked.connect(lambda: self.key.emit(KEY_BACK))
        self.home_btn.clicked.connect(lambda: self.key.emit(KEY_HOME))
        self.recents_btn.clicked.connect(lambda: self.recents_requested.emit())
        self.dpi_down_btn.clicked.connect(
            lambda: self.density_requested.emit(-DPI_STEP))
        self.dpi_up_btn.clicked.connect(
            lambda: self.density_requested.emit(DPI_STEP))
        self.window_btn.clicked.connect(
            lambda: self.window_toggle_requested.emit())
        self.close_btn.clicked.connect(lambda: self.close_requested.emit())
        self.toolbar.adjustSize()
        self.toolbar.move(12, 12)

        self.recents_panel = RecentsPanel(self)
        self.recents_panel.dismissed.connect(self.setFocus)

        self.locked = False
        self.lock_banner = QPushButton(
            "Phone locked or asleep. Click to unlock (Ctrl+U)", self)
        self.lock_banner.setObjectName("LockBanner")
        self.lock_banner.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lock_banner.clicked.connect(lambda: self.unlock_shortcut.emit())
        self.lock_banner.hide()

        self.toast = QLabel("", self)
        self.toast.setObjectName("Toast")
        self.toast.setWordWrap(True)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toolbar.adjustSize()
        self.toolbar.move(12, 12)
        self.recents_panel.setGeometry(self.rect())
        self._place_overlays()

    def _place_overlays(self):
        self.lock_banner.adjustSize()
        self.lock_banner.move(
            (self.width() - self.lock_banner.width()) // 2, 12)
        if self.toast.isVisible():
            self.toast.setMaximumWidth(max(200, int(self.width() * 0.7)))
            self.toast.adjustSize()
            self.toast.move((self.width() - self.toast.width()) // 2,
                            self.height() - self.toast.height() - 24)

    def set_locked(self, locked: bool):
        self.locked = locked
        if locked:
            self._place_overlays()
            self.lock_banner.show()
            self.lock_banner.raise_()
        else:
            self.lock_banner.hide()

    def show_toast(self, text: str):
        self.toast.setText(text)
        self.toast.setMaximumWidth(max(200, int(self.width() * 0.7)))
        self.toast.adjustSize()
        self.toast.move((self.width() - self.toast.width()) // 2,
                        self.height() - self.toast.height() - 24)
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(7000)

    def set_image(self, img: QImage):
        self.image = img
        self.update()

    def _image_rect(self):
        if self.image is None:
            return None
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        dw, dh = iw * scale, ih * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def _normalize(self, pos, clamp):
        r = self._image_rect()
        if r is None or r.width() == 0 or r.height() == 0:
            return None
        nx = (pos.x() - r.x()) / r.width()
        ny = (pos.y() - r.y()) / r.height()
        if not clamp and not (0 <= nx <= 1 and 0 <= ny <= 1):
            return None
        return min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        r = self._image_rect()
        if r is not None:
            p.drawImage(r, self.image)

    def mousePressEvent(self, e):
        self.setFocus()
        btn = e.button()
        if btn == Qt.MouseButton.RightButton:
            self.key.emit(KEY_BACK)       # right click = Back
            return
        if btn == Qt.MouseButton.MiddleButton:
            self.key.emit(KEY_HOME)       # middle click = Home
            return
        if btn != Qt.MouseButton.LeftButton:
            return
        n = self._normalize(e.position(), clamp=False)
        if n:
            self._pressed = True
            self._last_move = time.monotonic()
            self.touch.emit(ACTION_DOWN, *n)

    def mouseMoveEvent(self, e):
        if not self._pressed:
            return
        now = time.monotonic()
        if now - self._last_move < MOUSE_MOVE_INTERVAL:
            return  # throttle: release always sends the exact final position
        n = self._normalize(e.position(), clamp=True)
        if n:
            self._last_move = now
            self.touch.emit(ACTION_MOVE, *n)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            n = self._normalize(e.position(), clamp=True)
            if n:
                self.touch.emit(ACTION_UP, *n)

    def wheelEvent(self, e):
        n = self._normalize(e.position(), clamp=False)
        if n:
            self.scroll.emit(n[0], n[1], e.angleDelta().y() / 120.0)

    def keyPressEvent(self, e):
        key = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if key == Qt.Key.Key_F11:
            window = self.window()
            if window.isFullScreen():
                window.showNormal()
            else:
                window.showFullScreen()
            return

        if key == Qt.Key.Key_F9:                 # Recents
            self.recents_requested.emit()
            return

        if key == Qt.Key.Key_F10:                # game mode: W/A/S/D as keycodes
            self.game_mode = not self.game_mode
            return

        if key == Qt.Key.Key_Escape and self.recents_panel.isVisible():
            self.recents_panel.hide()
            return

        if ctrl and key == Qt.Key.Key_U:         # unlock dialog
            self.unlock_shortcut.emit()
            return

        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.density_requested.emit(DPI_STEP)    # bigger UI
            return
        if ctrl and key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.density_requested.emit(-DPI_STEP)   # smaller UI
            return
        if ctrl and key == Qt.Key.Key_0:
            self.density_requested.emit(0)           # reset
            return

        if ctrl and shift and key == Qt.Key.Key_W:   # window <-> maximize
            self.window_toggle_requested.emit()
            return

        if ctrl and shift:                       # window snapping
            direction = {
                Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
                Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
            }.get(key)
            if direction:
                self.snap_requested.emit(direction)
                return

        if mods & (Qt.KeyboardModifier.ControlModifier |
                   Qt.KeyboardModifier.AltModifier):
            return super().keyPressEvent(e)
        code = GAME_KEYS.get(key) if self.game_mode else None
        if code is None:
            code = SPECIAL_KEYS.get(key)
        if code is not None:
            self.key.emit(code)
        elif e.text() and e.text().isprintable():
            self.text.emit(e.text())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sharp HD Android Desktop")
        self.resize(1280, 720)

        self.setStyleSheet(SHARP_UI_STYLESHEET)

        self.view = VideoWidget()
        self.setCentralWidget(self.view)
        self.statusBar().hide()

        self.worker = StreamWorker()
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status.connect(self.on_status)
        self.worker.pin_requested.connect(self.on_pin_requested)
        self.worker.recents_ready.connect(self.view.recents_panel.show_items)
        self.worker.lock_changed.connect(self.view.set_locked)

        self.audio = (AudioPlayer()
                      if HAVE_AUDIO and AUDIO_MODE != "off" else None)
        if self.audio and self.audio.ok:
            self.worker.audio_data.connect(self.audio.write)
        self.view.mute_btn.clicked.connect(self.on_mute_toggle)
        self.view.density_requested.connect(self.worker.adjust_density)
        self.view.window_toggle_requested.connect(self.worker.toggle_window)
        self.view.close_requested.connect(self.worker.close_active)

        self.view.touch.connect(self.worker.send_touch)
        self.view.scroll.connect(self.worker.send_scroll)
        self.view.key.connect(self.worker.send_keycode)
        self.view.text.connect(self.worker.send_text)
        self.view.unlock_shortcut.connect(self.on_unlock_shortcut)
        self.view.snap_requested.connect(self.worker.snap_window)
        self.view.recents_requested.connect(self.on_recents_requested)
        self.view.recents_panel.chosen.connect(self.on_task_chosen)
        self.view.recents_panel.closed.connect(self.worker.close_app)

        self.view.screen_btn.clicked.connect(self.on_screen_toggle)

        self.worker.start()
        self.view.setFocus()

    def on_frame(self):
        img = self.worker.take_frame()
        if img is not None:
            self.view.set_image(img)

    def _ask_secret(self):
        secret, ok = QInputDialog.getText(
            self, "Unlock phone",
            "Enter PIN / password (leave empty to just wake the phone):",
            QLineEdit.EchoMode.Password)
        self.view.setFocus()
        return secret if ok else None

    def on_pin_requested(self):
        self.worker.provide_pin(self._ask_secret())

    def on_unlock_shortcut(self):
        secret = self._ask_secret()
        if secret is not None:
            self.worker.unlock_now(secret)

    def on_recents_requested(self):
        panel = self.view.recents_panel
        if panel.isVisible():
            panel.hide()
            self.view.setFocus()
            return
        panel.show_items(None)          # "Loading..." instantly
        self.worker.request_recents()

    def on_task_chosen(self, comp):
        self.view.recents_panel.hide()
        self.view.setFocus()
        self.worker.open_task(comp)

    def on_screen_toggle(self, checked):
        self.worker.set_display_power(not checked)
        self.view.screen_btn.setText("Screen On" if checked else "Screen Off")

    def on_mute_toggle(self, checked):
        if self.audio:
            self.audio.muted = checked
        self.view.mute_btn.setText("Unmute" if checked else "Mute")

    def on_status(self, s):
        print(s)
        self.view.show_toast(s)

    def closeEvent(self, e):
        self.worker.set_display_power(True)
        self.worker.stop()
        self.worker.wait(5000)
        if self.audio:
            self.audio.close()
        super().closeEvent(e)


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore_phone_settings()
        sys.exit(0)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())