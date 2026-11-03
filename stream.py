# stream_v3.py
import concurrent.futures
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
from PyQt6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation,
                          QRectF, Qt, QThread, QTimer, pyqtSignal)
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QPushButton, QVBoxLayout, QWidget)

SERVER_JAR = "scrcpy-server-v3.1"
SERVER_VERSION = "3.1"
REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
LOCAL_PORT = 27183
SCROLL_SIGN = 1

# Try "h265" on newer phones: same quality at lower bitrate, less heat.
VIDEO_CODEC = "h264"
DECODER_NAME = {"h264": "h264", "h265": "hevc"}[VIDEO_CODEC]

PRESETS = {
    "1080p": (1920, 1080, 240, 16_000_000),
    "1440p": (2560, 1440, 320, 28_000_000),
    "2160p": (3840, 2160, 480, 50_000_000),
}
RESOLUTION = "1440p"
DPI_OVERRIDE = None
DISPLAY_W, DISPLAY_H, DISPLAY_DPI, VIDEO_BITRATE = PRESETS[RESOLUTION]
if DPI_OVERRIDE:
    DISPLAY_DPI = DPI_OVERRIDE

APPLY_WM_DENSITY = False
VD_SYSTEM_DECORATIONS = True

MOUSE_MOVE_INTERVAL = 0.008
ANIMATION_SCALE = 0.5
ANIM_KEYS = ("window_animation_scale", "transition_animation_scale",
             "animator_duration_scale")

# "off" | "session" | "persistent"
FREEFORM_MODE = "off"
LAUNCH_FREEFORM = True

AUTO_WINDOW = True
WINDOW_FRACTION = (0.70, 0.78)
WINDOW_CASCADE = 56
RESTART_SYSTEMUI_AFTER_FREEFORM = True
BACKUP_PATH = Path.home() / ".stream_v3_phone_backup.json"

AUDIO_MODE = "auto"   # "auto" | "pc" | "both" | "off"

LOCK_POLL_INTERVAL = 1.5
DPI_STEP, DPI_MIN, DPI_MAX = 20, 160, 640
META_CTRL_ON = 0x1000
FREEFORM_SETTINGS = {
    "enable_freeform_support": "1",
    "force_resizable_activities": "1",
    "freeform_window_management": "1",
    "force_desktop_mode_on_external_displays": "1",
}

MSG_KEYCODE, MSG_TEXT, MSG_TOUCH, MSG_SCROLL, MSG_DISPLAY_POWER = 0, 1, 2, 3, 10
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_MOUSE = -1
KEY_HOME, KEY_BACK, KEY_APP_SWITCH = 3, 4, 187

SPECIAL_KEYS = {
    Qt.Key.Key_Return: 66, Qt.Key.Key_Enter: 66,
    Qt.Key.Key_Backspace: 67, Qt.Key.Key_Delete: 112,
    Qt.Key.Key_Tab: 61, Qt.Key.Key_Escape: KEY_BACK,
    Qt.Key.Key_Up: 19, Qt.Key.Key_Down: 20,
    Qt.Key.Key_Left: 21, Qt.Key.Key_Right: 22,
    Qt.Key.Key_Home: 122, Qt.Key.Key_End: 123,
    Qt.Key.Key_PageUp: 92, Qt.Key.Key_PageDown: 93,
}

GAME_KEYS = {Qt.Key.Key_W: 51, Qt.Key.Key_A: 29,
             Qt.Key.Key_S: 32, Qt.Key.Key_D: 30}

try:
    from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

DECODE_ERRORS = tuple(c for c in (getattr(av.error, "FFmpegError", None),
                                  av.error.InvalidDataError) if c)

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
    """Restore recorded values ('null'/'empty' = unset)."""
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
        saved = {k: "null" for k in ANIM_KEYS}
        print("No backup found; resetting animation speeds only")
    _apply_saved(d, saved)
    _write_backup({"transient": {}, "persistent": {}})
    print("Phone settings restored:", ", ".join(saved))


def diagnose(out_path=None):
    """CLI: python stream_v3.py --diagnose"""
    devices = adb.device_list()
    if not devices:
        print("No device found")
        return
    d = devices[0]
    out_path = Path(out_path) if out_path else Path.home() / "stream_diagnose.txt"
    pat = "freeform|resizable|desktop|multi_window|multiwindow|taskbar"
    sections = [
        ("device",
         "getprop ro.product.model; getprop ro.build.version.release; "
         "getprop ro.build.version.sdk; getprop ro.build.version.oneui"),
        ("settings global", f"settings list global | grep -iE '{pat}'"),
        ("settings secure", f"settings list secure | grep -iE '{pat}'"),
        ("settings system", f"settings list system | grep -iE '{pat}'"),
        ("scrcpy display", "dumpsys display | grep -i scrcpy | head -n 20"),
        ("tasks (all displays)",
         r"dumpsys activity activities | grep -E "
         r"'Display #|Display [0-9]+|Task\{|ActivityRecord\{' | head -n 150"),
        ("am help (task/window)",
         "am help 2>&1 | grep -iE 'task|windowing|display'"),
        ("cmd window help",
         "cmd window help 2>&1 | grep -iE 'freeform|windowing|desktop|density|size'"),
        ("wm shell help", "wm shell help 2>&1 | head -n 40"),
        ("event log tags",
         "logcat -b events -d -t 300 2>&1 | "
         "grep -E 'wm_create_activity|wm_task_created|wm_create_task' | tail -n 5"),
    ]
    parts = []
    for title, cmd in sections:
        try:
            out = d.shell(cmd).strip()
        except Exception as e:
            out = f"(failed: {e!r})"
        parts.append(f"=== {title} ===\n{out[:6000] or '(empty)'}\n")
    out_path.write_text("\n".join(parts))
    print(f"Wrote {out_path}")


def _split_meta(buf: bytearray):
    """[u64 pts/flags][u32 size][payload] stream; flag bit63 = codec config."""
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


class AudioPlayer:
    """Phone audio on this computer (48 kHz, 16-bit, stereo)."""

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
            self.sink.setBufferSize(48000 * 4 // 8)
            self.io = self.sink.start()
            self.ok = self.io is not None
        except Exception as e:
            print(f"Audio output unavailable: {e!r}")

    def write(self, data: bytes):
        if not self.ok or self.muted:
            return
        n = min(len(data), self.sink.bytesFree()) & ~3
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
    lock_changed = pyqtSignal(bool)
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
        self._new_task_event = threading.Event()
        self._event_tids = set()
        self._jobs = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def stop(self):
        self._running = False
        self._pin_event.set()
        self._jobs.shutdown(wait=False, cancel_futures=True)

    def _submit(self, fn):
        if not self._running:
            return
        try:
            self._jobs.submit(fn)
        except RuntimeError:
            pass

    # ---- latest-frame-only hand-off ----
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

    # ---- device settings ----
    def _set_global(self, d, key, value, kind):
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

    # ---- device info ----
    def _probe_device(self, d):
        try:
            self.sdk = int(d.shell("getprop ro.build.version.sdk").strip() or 0)
        except ValueError:
            self.sdk = 0
        model = d.shell("getprop ro.product.model").strip()
        self.status.emit(f"Device: {model} (Android API {self.sdk})")
        out = d.shell("input -d 0 keyevent 0")
        self._input_cmd = ("input" if re.search(r"sage|rror|nknown", out)
                           else "input -d 0")

    def _dump_window_settings(self, d):
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

    # ---- server startup ----
    def _start_server(self, d, scid):
        d.sync.push(SERVER_JAR, REMOTE_JAR)
        d.forward(f"tcp:{LOCAL_PORT}", f"localabstract:scrcpy_{scid:08x}")

        extra = "" if VD_SYSTEM_DECORATIONS else " vd_system_decorations=false"
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
            f"{audio_opts} control=true max_size=0 video_codec={VIDEO_CODEC} "
            f"video_bit_rate={VIDEO_BITRATE} max_fps=60 stay_awake=true "
            f"send_device_meta=false send_codec_meta=false send_frame_meta=true "
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
                if (flags >> 63) & 1:
                    continue
                if payload:
                    self.audio_data.emit(payload)

    def _connect_control(self):
        s = socket.socket()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.connect(("127.0.0.1", LOCAL_PORT))
        return s

    # ---- lock screen ----
    @staticmethod
    def _is_awake(d) -> bool:
        out = d.shell(
            "dumpsys power | grep -o 'mWakefulness=[A-Za-z]*' | head -n 1")
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
        time.sleep(0.3)

    def provide_pin(self, pin):
        self._pin = pin
        self._pin_event.set()

    def _unlock(self, d, secret: str) -> bool:
        inp = self._input_cmd
        self._wake(d)
        d.shell("wm dismiss-keyguard")
        time.sleep(0.7)
        if not self._is_locked(d):
            return True
        if not secret:
            return False
        w, h = self._screen_size(d)
        d.shell(f"{inp} swipe {w // 2} {int(h * 0.85)} "
                f"{w // 2} {int(h * 0.25)} 250")
        time.sleep(0.8)
        if secret.isdigit():
            script = "; sleep 0.07; ".join(
                f"{inp} keyevent {7 + int(c)}" for c in secret)
        else:
            script = f"{inp} text {shlex.quote(secret.replace(' ', '%s'))}"
        d.shell(script)
        time.sleep(0.35)
        if self._is_locked(d):
            d.shell(f"{inp} keyevent 66")
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
        self._fail_count += 1
        if self._fail_count >= 3:
            self._block_until = time.monotonic() + 60
            self._fail_count = 0
        self.status.emit("Unlock failed: " + self._lock_debug(d))
        return False

    def _ensure_unlocked(self, d):
        self._wake(d)
        if self._is_locked(d):
            d.shell("wm dismiss-keyguard")
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

        self._submit(job)

    def _watch_lock(self, d):
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

    # ---- tasks on the virtual display ----
    @staticmethod
    def _dump_tasks(d):
        return d.shell(
            "dumpsys activity activities | "
            r"grep -E 'Display #|Task\{|ActivityRecord\{'")

    def _task_table(self, d):
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
        return [(t, c) for t, c, _m in self._task_table(d)
                if not self._is_system(c)]

    # ---- windowing ----
    def _launch(self, d, comp, windowing=None):
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
        out = self._launch(d, comp, 5)
        if re.search(r"Unknown option|Error:", out):
            return False
        time.sleep(0.35)
        box = self._bounds_for(self._cascade)
        self._cascade += 1
        d.shell("am task resize %d %d %d %d %d" % ((tid,) + box))
        time.sleep(0.4)
        for t, c, mode in self._task_table(d):
            if t == tid or c == comp:
                return mode not in ("fullscreen", "?")
        return False

    def _watch_new_tasks(self, d):
        try:
            conn = d.shell(
                "logcat -b events -T 1 wm_create_activity:I *:S", stream=True)
        except Exception:
            return
        buf = ""
        try:
            while self._running:
                chunk = conn.read(1024)
                if not chunk:
                    break
                buf += (chunk.decode("utf-8", "ignore")
                        if isinstance(chunk, bytes) else chunk)
                *lines, buf = buf.split("\n")
                for ln in lines:
                    m = re.search(r"wm_create_activity.*?\[\d+,-?\d+,(\d+),", ln)
                    if not m:
                        continue
                    tid = int(m.group(1))
                    if tid not in self._seen_tasks and tid not in self._event_tids:
                        self._event_tids.add(tid)
                        self._new_task_event.set()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _auto_window_loop(self, d):
        first = True
        while self._running and self._auto_window_on:
            woke = self._new_task_event.wait(2.0)
            self._new_task_event.clear()
            if woke:
                time.sleep(0.12)
            try:
                if self._get_display_id(d) is None:
                    continue
                table = self._task_table(d)
                if woke and not first:
                    for _ in range(3):
                        if any(t not in self._seen_tasks for t, _c, _m in table):
                            break
                        time.sleep(0.15)
                        table = self._task_table(d)
                if first:
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

        self._submit(job)

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

        self._submit(job)

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

        self._submit(job)

    def _top_app(self, d):
        for t, c, m in self._task_table(d):
            if not self._is_system(c):
                return t, c, m
        return None

    def snap_window(self, direction: str):
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

        self._submit(job)

    def toggle_window(self):
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

        self._submit(job)

    def close_active(self):
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

        self._submit(job)

    def adjust_density(self, delta: int):
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

        self._submit(job)

    # ---- control messages ----
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

    def send_keycode(self, keycode, meta=0):
        for action in (ACTION_DOWN, ACTION_UP):
            self._send(struct.pack(">BBiii", MSG_KEYCODE, action,
                                   keycode, 0, meta))

    def send_text(self, text):
        raw = text.encode("utf-8")[:300]
        self._send(struct.pack(">BI", MSG_TEXT, len(raw)) + raw)

    def set_display_power(self, on: bool):
        self.display_off = not on
        self._send(struct.pack(">BB", MSG_DISPLAY_POWER, 1 if on else 0))

    # ---- main loop ----
    @staticmethod
    def _make_decoder():
        codec = av.CodecContext.create(DECODER_NAME, "r")
        try:
            codec.thread_type = "SLICE"
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

            self._backup = _read_backup()
            if self._backup["transient"]:
                self._restore_transient(d)

            self._probe_device(d)
            self._dump_window_settings(d)

            self._ensure_unlocked(d)
            if not self._running:
                return

            self._apply_desktop_settings(d)

            self._start_server(d, random.randint(1, 0x7FFFFFFF))
            sock = self._connect_video()
            if self._audio_mode:
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
                threading.Thread(target=self._watch_new_tasks, args=(d,),
                                 daemon=True).start()
            self._apply_density_override(d)
            sock.settimeout(0.2)
            self.status.emit(
                f"Connected ({DISPLAY_W}x{DISPLAY_H} @ {DISPLAY_DPI} dpi, "
                f"{VIDEO_CODEC}, audio: {self._audio_mode or 'off'})")

            codec = None   # opened lazily on the first (config) packet
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

                buf += data
                packets = []
                for flags, payload in _split_meta(buf):
                    if (flags >> 63) & 1:      # codec config packet
                        if codec is None:
                            codec = self._make_decoder()
                            codec.extradata = payload
                            codec.open()
                        continue
                    if payload and codec is not None:
                        pkt = av.Packet(payload)
                        pkt.pts = pkt.dts = flags & 0x7FFFFFFFFFFFFFFF
                        packets.append(pkt)

                # Decode all (reference frames) but convert only the newest.
                last = None
                if codec is not None:
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
                                 QImage.Format.Format_RGB32).copy()
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


def _draw_icon(p, kind, rect, on=False, size=None):
    u = (size or min(rect.width(), rect.height())) / 40.0
    cx, cy = rect.center().x(), rect.center().y()

    def P(x, y):
        return QPointF(cx + x * u, cy + y * u)

    def R(x, y, w, h):
        return QRectF(cx + x * u, cy + y * u, w * u, h * u)

    pen = QPen(QColor(255, 255, 255, 240))
    pen.setWidthF(max(1.6, 1.9 * u))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if kind in ("chevron_right", "chevron_left", "back"):
        k = 1 if kind == "chevron_right" else -1
        p.drawPolyline(QPolygonF([P(-3 * k, -8), P(4 * k, 0), P(-3 * k, 8)]))
    elif kind == "home":
        p.drawEllipse(R(-7, -7, 14, 14))
    elif kind == "recents":
        for x in (-6, 0, 6):
            p.drawLine(P(x, -7), P(x, 7))
    elif kind == "power":
        p.drawArc(R(-7, -7, 14, 14), 120 * 16, 300 * 16)
        p.drawLine(P(0, -10), P(0, -1))
    elif kind == "volume":
        p.drawPolygon(QPolygonF([P(-9, -3), P(-5, -3), P(0, -7),
                                 P(0, 7), P(-5, 3), P(-9, 3)]))
        if on:
            p.drawLine(P(4, -3), P(9, 3))
            p.drawLine(P(9, -3), P(4, 3))
        else:
            p.drawArc(R(-5, -5, 10, 10), -55 * 16, 110 * 16)
            p.drawArc(R(-9, -9, 18, 18), -50 * 16, 100 * 16)
    elif kind in ("zoom_in", "zoom_out"):
        p.drawEllipse(R(-8, -8, 12, 12))
        p.drawLine(P(2.5, 2.5), P(8.5, 8.5))
        p.drawLine(P(-5, -2), P(1, -2))
        if kind == "zoom_in":
            p.drawLine(P(-2, -5), P(-2, 1))
    elif kind == "window":
        p.drawRect(R(-8, -3, 11, 11))
        p.drawPolyline(QPolygonF([P(-3, -3), P(-3, -8), P(8, -8),
                                  P(8, 3), P(3, 3)]))
    elif kind == "close":
        p.drawLine(P(-6, -6), P(6, 6))
        p.drawLine(P(6, -6), P(-6, 6))


class IconButton(QPushButton):
    def __init__(self, kind, tip, w=40, h=40, checkable=False, ghost=False,
                 icon_size=None):
        super().__init__()
        self.kind = kind
        self._hover = False
        self._ghost = ghost
        self._icon_size = icon_size
        self.setToolTip(tip)
        self.setFixedSize(w, h)
        self.setCheckable(checkable)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)

    def set_kind(self, kind):
        self.kind = kind
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.isDown():
            bg = QColor(11, 87, 208, 255)
        elif self.isChecked():
            bg = QColor(11, 87, 208, 200)
        elif self._hover:
            bg = QColor(255, 255, 255, 46)
        elif self._ghost:
            bg = QColor(20, 20, 25, 190)
        else:
            bg = QColor(0, 0, 0, 0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 9, 9)
        _draw_icon(p, self.kind, QRectF(self.rect()), self.isChecked(),
                   self._icon_size)


class IconBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(6, 8, 6, 8)
        self._lay.setSpacing(4)

    def add(self, btn):
        self._lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)

    def add_sep(self):
        sep = QWidget()
        sep.setFixedSize(24, 1)
        sep.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sep.setStyleSheet("background: rgba(255, 255, 255, 45);")
        self._lay.addWidget(sep, 0, Qt.AlignmentFlag.AlignHCenter)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(20, 20, 25, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                          14, 14)


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
        self.hide()
        self.dismissed.emit()
        e.accept()

    def wheelEvent(self, e):
        e.accept()


class VideoWidget(QWidget):
    touch = pyqtSignal(int, float, float)
    scroll = pyqtSignal(float, float, float)
    key = pyqtSignal(int)
    keymod = pyqtSignal(int, int)
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

        self._bar_open = False
        self._anim = None
        self.bar = IconBar(self)
        self.back_btn = IconButton("back", "Back  (right-click)")
        self.home_btn = IconButton("home", "Home  (middle-click)")
        self.recents_btn = IconButton("recents", "Recent apps  (F9)")
        self.window_btn = IconButton(
            "window", "Window / maximize  (Ctrl+Shift+W)")
        self.close_btn = IconButton("close", "Close app")
        self.dpi_down_btn = IconButton("zoom_out", "Smaller UI  (Ctrl+-)")
        self.dpi_up_btn = IconButton("zoom_in", "Bigger UI  (Ctrl++)")
        self.mute_btn = IconButton(
            "volume", "Mute computer audio", checkable=True)
        self.screen_btn = IconButton(
            "power", "Turn phone screen off", checkable=True)
        for b in (self.back_btn, self.home_btn, self.recents_btn):
            self.bar.add(b)
        self.bar.add_sep()
        for b in (self.window_btn, self.close_btn):
            self.bar.add(b)
        self.bar.add_sep()
        for b in (self.dpi_down_btn, self.dpi_up_btn):
            self.bar.add(b)
        self.bar.add_sep()
        for b in (self.mute_btn, self.screen_btn):
            self.bar.add(b)
        self.bar.adjustSize()
        self.bar.hide()

        self.handle = IconButton("chevron_right", "Show controls  (F8)",
                                 24, 64, ghost=True, icon_size=40)
        self.handle.clicked.connect(lambda: self.toggle_bar())

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
        self.recents_panel.setGeometry(self.rect())
        self._place_overlays()

    def _bar_pos(self, open_):
        y = max(8, (self.height() - self.bar.height()) // 2)
        x = self.handle.width() + 6 if open_ else -self.bar.width() - 6
        return QPoint(x, y)

    def toggle_bar(self, open_=None):
        self._bar_open = (not self._bar_open) if open_ is None else open_
        self.handle.set_kind("chevron_left" if self._bar_open
                             else "chevron_right")
        self.handle.setToolTip(
            "Hide controls  (F8)" if self._bar_open else "Show controls  (F8)")
        if self._anim is not None:
            self._anim.stop()
        self.bar.adjustSize()
        if self._bar_open:
            if not self.bar.isVisible():
                self.bar.move(self._bar_pos(False))
            self.bar.show()
            self.bar.raise_()
            self.handle.raise_()
        anim = QPropertyAnimation(self.bar, b"pos", self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.bar.pos())
        anim.setEndValue(self._bar_pos(self._bar_open))
        anim.finished.connect(
            lambda: self.bar.hide() if not self._bar_open else None)
        self._anim = anim
        anim.start()

    def _place_bar(self):
        self.handle.move(0, max(8, (self.height() - self.handle.height()) // 2))
        self.handle.raise_()
        self.bar.adjustSize()
        if self._bar_open:
            self.bar.move(self._bar_pos(True))

    def _place_overlays(self):
        self._place_bar()
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
            self.key.emit(KEY_BACK)
            return
        if btn == Qt.MouseButton.MiddleButton:
            self.key.emit(KEY_HOME)
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
            return
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

        if key == Qt.Key.Key_F8:
            self.toggle_bar()
            return

        if key == Qt.Key.Key_F9:
            self.recents_requested.emit()
            return

        if key == Qt.Key.Key_F10:
            self.game_mode = not self.game_mode
            return

        if key == Qt.Key.Key_Escape and self.recents_panel.isVisible():
            self.recents_panel.hide()
            return

        if ctrl and key == Qt.Key.Key_U:
            self.unlock_shortcut.emit()
            return

        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.density_requested.emit(DPI_STEP)
            return
        if ctrl and key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.density_requested.emit(-DPI_STEP)
            return
        if ctrl and key == Qt.Key.Key_0:
            self.density_requested.emit(0)
            return

        if ctrl and shift and key == Qt.Key.Key_W:
            self.window_toggle_requested.emit()
            return

        if ctrl and shift:
            direction = {
                Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
                Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
            }.get(key)
            if direction:
                self.snap_requested.emit(direction)
                return

        # Ctrl+letter as real Android key events with the Ctrl meta state
        if ctrl and not shift and 65 <= key <= 90:   # A-Z
            self.keymod.emit(key - 65 + 29, META_CTRL_ON)
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
        self.view.keymod.connect(self.worker.send_keycode)
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
        panel.show_items(None)
        self.worker.request_recents()

    def on_task_chosen(self, comp):
        self.view.recents_panel.hide()
        self.view.setFocus()
        self.worker.open_task(comp)

    def on_screen_toggle(self, checked):
        self.worker.set_display_power(not checked)
        self.view.screen_btn.setToolTip(
            "Turn phone screen on" if checked else "Turn phone screen off")

    def on_mute_toggle(self, checked):
        if self.audio:
            self.audio.muted = checked
        self.view.mute_btn.setToolTip(
            "Unmute computer audio" if checked else "Mute computer audio")

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
    if "--diagnose" in sys.argv:
        diagnose()
        sys.exit(0)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())