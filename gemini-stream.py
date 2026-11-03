# stream.py - Android virtual desktop (scrcpy-server v3.1, new_display)
# Low-latency decode + 1440p + freeform windows + custom Recents + unlock flow
import random
import re
import shlex
import socket
import struct
import sys
import threading
import time

import av
from adbutils import adb
from PyQt6.QtCore import QRectF, Qt, QThread, pyqtSignal
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
# Quality / performance config
# ----------------------------------------------------------------------
# name: (width, height, dpi, bitrate). Every preset keeps the same UI size
# (dp width = px * 160 / dpi = 1280dp); higher presets are just sharper
# and cost more encode/decode time. Drop to "1080p" if it gets laggy.
PRESETS = {
    "1080p": (1920, 1080, 240, 16_000_000),
    "1440p": (2560, 1440, 320, 28_000_000),
    "2160p": (3840, 2160, 480, 50_000_000),
}
RESOLUTION = "1440p"
DPI_OVERRIDE = None   # e.g. 280 -> bigger UI, 360 -> smaller UI (at 1440p)
DISPLAY_W, DISPLAY_H, DISPLAY_DPI, VIDEO_BITRATE = PRESETS[RESOLUTION]
if DPI_OVERRIDE:
    DISPLAY_DPI = DPI_OVERRIDE

MAX_FPS = 60
# True: scrcpy sends packet headers, so we skip the H.264 parser (which waits
# for the next frame before releasing the current one = +1 frame of latency).
USE_FRAME_META = True
# Mouse-move throttle (seconds). Stops hi-rate mice flooding the phone's
# input queue, a common cause of touch delay.
MOUSE_MOVE_INTERVAL = 0.008
# 1.0 = default. 0.5 = twice as fast animations. None = leave untouched.
# Applied at start and restored on exit (global setting on the phone).
ANIMATION_SCALE = 0.5

# Freeform "desktop mode" so windows get title bars you can drag/resize.
# Needs a one-time phone reboot after the first run on most devices.
ENABLE_FREEFORM = True
# Fallback only: force density via `wm density -d <id>`.
APPLY_WM_DENSITY = False
# False -> passes vd_system_decorations=false (hides system navbar/launcher).
VD_SYSTEM_DECORATIONS = True

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
    Qt.Key.Key_W: 51, Qt.Key.Key_A: 29,
    Qt.Key.Key_S: 32, Qt.Key.Key_D: 30,
}

ANIM_KEYS = ("window_animation_scale", "transition_animation_scale",
             "animator_duration_scale")

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
    font-family: 'Segoe UI', sans-serif;
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
QWidget#RecentsPanel {
    background-color: rgba(0, 0, 0, 190);
}
QLabel#RecentsTitle {
    color: #ffffff;
    font-family: 'Segoe UI', sans-serif;
    font-size: 20px;
    font-weight: 600;
    padding-bottom: 12px;
}
QLabel#RecentsEmpty {
    color: rgba(255, 255, 255, 170);
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}
QFrame#RecentsCard {
    background-color: rgba(32, 33, 40, 245);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 12px;
}
QPushButton#CardBtn {
    background: transparent;
    color: #ffffff;
    border: none;
    text-align: left;
    padding: 14px 16px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#CardBtn:hover {
    background-color: rgba(255, 255, 255, 25);
    border-radius: 12px;
}
QPushButton#CardClose {
    background: transparent;
    color: rgba(255, 255, 255, 160);
    border: none;
    padding: 6px 12px;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#CardClose:hover {
    color: #ff6b6b;
}
"""


def _extract_packets(buf: bytearray):
    """Split scrcpy frame-meta stream: [u64 pts/flags][u32 size][payload]."""
    pkts = []
    while len(buf) >= 12:
        size = struct.unpack_from(">I", buf, 8)[0]
        if len(buf) < 12 + size:
            break
        payload = bytes(buf[12:12 + size])
        del buf[:12 + size]
        if payload:
            pkts.append(av.Packet(payload))
    return pkts


class StreamWorker(QThread):
    frame_ready = pyqtSignal()
    status = pyqtSignal(str)
    pin_requested = pyqtSignal()
    recents_ready = pyqtSignal(list)

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
        self._saved_anim = {}

    def stop(self):
        self._running = False
        self._pin_event.set()  # release any pending PIN wait

    # ------------------------------------------------------------------
    # Frame hand-off (latest-frame-wins: UI never builds a backlog)
    # ------------------------------------------------------------------
    def _publish(self, img):
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
    # Phone-side setup
    # ------------------------------------------------------------------
    def _apply_desktop_settings(self, d):
        if not ENABLE_FREEFORM:
            return
        cur = d.shell(
            "settings get global force_desktop_mode_on_external_displays"
        ).strip()
        if cur == "1":
            return
        for cmd in (
            "settings put global enable_freeform_support 1",
            "settings put global force_resizable_activities 1",
            "settings put global freeform_window_management 1",
            "settings put global force_desktop_mode_on_external_displays 1",
        ):
            d.shell(cmd)
        self.status.emit(
            "Freeform mode enabled. If windows have no title bars, "
            "reboot the phone once and reconnect.")

    def _set_animation_scale(self, d):
        if ANIMATION_SCALE is None:
            return
        for k in ANIM_KEYS:
            self._saved_anim[k] = d.shell(f"settings get global {k}").strip()
            d.shell(f"settings put global {k} {ANIMATION_SCALE}")

    def _restore_animation_scale(self, d):
        for k, v in self._saved_anim.items():
            try:
                if v in ("", "null"):
                    d.shell(f"settings delete global {k}")
                else:
                    d.shell(f"settings put global {k} {v}")
            except Exception:
                pass
        self._saved_anim = {}

    # ------------------------------------------------------------------
    # Server startup
    # ------------------------------------------------------------------
    def _start_server(self, d, scid):
        d.sync.push(SERVER_JAR, REMOTE_JAR)
        d.forward(f"tcp:{LOCAL_PORT}", f"localabstract:scrcpy_{scid:08x}")

        extra = "" if VD_SYSTEM_DECORATIONS else " vd_system_decorations=false"
        meta = "true" if USE_FRAME_META else "false"
        cmd = (
            f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server "
            f"{SERVER_VERSION} scid={scid:08x} log_level=info tunnel_forward=true "
            f"audio=false control=true max_size=0 video_bit_rate={VIDEO_BITRATE} "
            f"max_fps={MAX_FPS} send_device_meta=false send_codec_meta=false "
            f"send_frame_meta={meta} "
            f"new_display={DISPLAY_W}x{DISPLAY_H}/{DISPLAY_DPI}{extra}"
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
        NEVER run `wm density` without -d: that changes the real screen."""
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
    # Lock screen handling (keyguard lives on the physical display 0)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_awake(d) -> bool:
        return "mWakefulness=Awake" in d.shell("dumpsys power")

    @staticmethod
    def _is_locked(d) -> bool:
        out = d.shell("dumpsys window")
        return bool(re.search(
            r"(mKeyguardShowing|isKeyguardShowing|mShowingLockscreen"
            r"|mDreamingLockscreen)=true", out))

    def _wake(self, d):
        if not self._is_awake(d):
            d.shell("input keyevent KEYCODE_WAKEUP")
            for _ in range(30):
                if self._is_awake(d):
                    break
                time.sleep(0.1)
        time.sleep(0.25)

    def provide_pin(self, pin):
        """Called from the UI thread. None = user cancelled."""
        self._pin = pin
        self._pin_event.set()

    def _unlock(self, d, secret: str) -> bool:
        if secret.isdigit():
            keys = ";".join(f"input keyevent {7 + int(c)}" for c in secret)
        else:
            keys = "input text " + shlex.quote(secret.replace(" ", "%s"))
        script = (
            "wm dismiss-keyguard; sleep 0.4; "
            "input keyevent 82; sleep 0.2; "
            f"{keys}; sleep 0.15; input keyevent 66"
        )
        d.shell(script)
        for _ in range(20):
            time.sleep(0.15)
            if not self._is_locked(d):
                return True
        return False

    def _ensure_unlocked(self, d):
        self._wake(d)
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
            if self._unlock(d, secret):
                self.status.emit("Unlocked")
                return
            self.status.emit("Unlock failed")

    def unlock_now(self, secret: str):
        """Mid-session unlock (Ctrl+U). Runs off the UI thread."""
        d = self.device
        if not d or not secret:
            return

        def job():
            try:
                self._wake(d)
                if not self._is_locked(d):
                    self.status.emit("Device already unlocked")
                    return
                ok = self._unlock(d, secret)
                self.status.emit("Unlocked" if ok else "Unlock failed")
            except Exception as e:
                self.status.emit(f"Unlock error: {e!r}")

        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------------
    # Tasks / Recents / window snapping (all best-effort, parsed from
    # dumpsys, whose format varies a little between Android versions)
    # ------------------------------------------------------------------
    def _list_tasks(self, d):
        """Returns [(task_id, 'pkg/.Activity'), ...] on the virtual display,
        top-most first."""
        did = self._get_display_id(d)
        if did is None:
            return []
        out = d.shell("dumpsys activity activities")
        tasks = {}
        cur_display = None
        for line in out.splitlines():
            m = re.search(r"Display #(\d+)", line)
            if m:
                cur_display = int(m.group(1))
                continue
            if cur_display != did:
                continue
            m = re.search(r"ActivityRecord\{\w+ u\d+ ([\w.$]+/[\w.$]+) t(\d+)",
                          line)
            if m:
                comp, tid = m.group(1), int(m.group(2))
                if tid not in tasks:
                    tasks[tid] = comp
        skip = ("launcher", "systemui", "nexuslauncher", "quickstep")
        return [(tid, comp) for tid, comp in tasks.items()
                if not any(s in comp.lower() for s in skip)]

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
                did = self._get_display_id(d)
                disp = f"--display {did} " if did is not None else ""
                # Same flags the launcher uses: resumes the existing task
                d.shell(
                    f"am start {disp}-f 0x10200000 "
                    "-a android.intent.action.MAIN "
                    "-c android.intent.category.LAUNCHER "
                    f"-n {shlex.quote(comp)}")
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

    def snap_window(self, direction: str):
        """Ctrl+Shift+Arrows: left/right half, maximize, centered."""
        d = self.device
        if not d:
            return

        def job():
            try:
                tasks = self._list_tasks(d)
                if not tasks:
                    self.status.emit("No window to move")
                    return
                tid = tasks[0][0]
                W, H = DISPLAY_W, DISPLAY_H
                boxes = {
                    "left": (0, 0, W // 2, H),
                    "right": (W // 2, 0, W, H),
                    "up": (0, 0, W, H),
                    "down": (W // 5, H // 6, W * 4 // 5, H * 5 // 6),
                }
                l, t, r, b = boxes[direction]
                out = d.shell(f"am task resize {tid} {l} {t} {r} {b}")
                if re.search(r"rror|nknown|xception", out):
                    out = d.shell(
                        f"cmd activity task resize {tid} {l} {t} {r} {b}")
                if re.search(r"rror|nknown|xception", out):
                    self.status.emit(
                        "Snap not supported here (needs freeform mode)")
            except Exception as e:
                self.status.emit(f"Snap error: {e!r}")

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
    def _make_decoder(self):
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

            self._ensure_unlocked(d)
            if not self._running:
                return
            self._apply_desktop_settings(d)
            self._set_animation_scale(d)

            self._start_server(d, random.randint(1, 0x7FFFFFFF))
            sock = self._connect_video()
            self.control_sock = self._connect_control()
            self._apply_density_override(d)
            sock.settimeout(1.0)
            self.status.emit(
                f"Connected ({DISPLAY_W}x{DISPLAY_H} @ {DISPLAY_DPI} dpi)")

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
                # convert + show the newest frame of this batch.
                last = None
                for packet in packets:
                    try:
                        frames = codec.decode(packet)
                    except DECODE_ERRORS:
                        continue
                    if frames:
                        last = frames[-1]
                if last is None:
                    continue

                arr = last.to_ndarray(format="bgra")
                h, w, _ = arr.shape
                self.frame_w, self.frame_h = w, h
                img = QImage(arr.data, w, h, 4 * w, QImage.Format.Format_RGB32)
                img._keep = arr  # keep the buffer alive without a copy
                self._publish(img)
        except Exception as e:
            self.status.emit(f"Error: {e!r}")
        finally:
            if self.display_off:
                self.set_display_power(True)
            for s in (sock, self.control_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            if d:
                self._restore_animation_scale(d)
                try:
                    d.forward_remove(f"tcp:{LOCAL_PORT}")
                except Exception:
                    pass


class RecentsPanel(QWidget):
    chosen = pyqtSignal(str)
    closed = pyqtSignal(str)
    dismissed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("RecentsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 72, 48, 48)
        title = QLabel("Recent apps")
        title.setObjectName("RecentsTitle")
        outer.addWidget(title)
        self._grid = QGridLayout()
        self._grid.setSpacing(14)
        outer.addLayout(self._grid)
        outer.addStretch(1)
        self.hide()

    @staticmethod
    def _pretty(comp):
        pkg = comp.split("/")[0]
        return pkg.split(".")[-1].capitalize(), pkg

    def populate(self, items):
        while self._grid.count():
            w = self._grid.takeAt(0).widget()
            if w:
                w.deleteLater()
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
            btn.clicked.connect(lambda _=False, c=comp: self.chosen.emit(c))
            x = QPushButton("X")
            x.setObjectName("CardClose")
            x.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            x.clicked.connect(lambda _=False, c=comp: self.closed.emit(c))
            lay.addWidget(btn, 1)
            lay.addWidget(x, 0)
            self._grid.addWidget(card, i // 4, i % 4)

    def show_items(self, items):
        self.populate(items)
        self.show()
        self.raise_()

    def mousePressEvent(self, e):
        self.dismissed.emit()

    def wheelEvent(self, e):
        e.accept()  # don't leak scrolling to the phone underneath


class VideoWidget(QWidget):
    touch = pyqtSignal(int, float, float)
    scroll = pyqtSignal(float, float, float)
    key = pyqtSignal(int)
    text = pyqtSignal(str)
    unlock_shortcut = pyqtSignal()
    recents_requested = pyqtSignal()
    snap_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.image = None
        self._pressed = False
        self._last_move = 0.0

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
        self.back_btn = mk("Back", 64)
        self.home_btn = mk("Home", 64)
        self.recents_btn = mk("Recents", 80)
        self.back_btn.clicked.connect(lambda: self.key.emit(KEY_BACK))
        self.home_btn.clicked.connect(lambda: self.key.emit(KEY_HOME))
        self.recents_btn.clicked.connect(self.recents_requested.emit)

        self.recents_panel = RecentsPanel(self)
        self.recents_panel.dismissed.connect(self.recents_panel.hide)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toolbar.adjustSize()
        self.toolbar.move(12, 12)
        self.recents_panel.setGeometry(self.rect())

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
        if btn == Qt.MouseButton.RightButton:      # right click = Back
            self.key.emit(KEY_BACK)
            return
        if btn == Qt.MouseButton.MiddleButton:     # middle click = Home
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

        if key == Qt.Key.Key_F9:                  # Recents
            self.recents_requested.emit()
            return

        if self.recents_panel.isVisible() and key == Qt.Key.Key_Escape:
            self.recents_panel.hide()
            return

        if ctrl and key == Qt.Key.Key_U:          # Unlock dialog
            self.unlock_shortcut.emit()
            return

        if ctrl and shift:                        # Window snapping
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

        self.view.touch.connect(self.worker.send_touch)
        self.view.scroll.connect(self.worker.send_scroll)
        self.view.key.connect(self.worker.send_keycode)
        self.view.text.connect(self.worker.send_text)
        self.view.unlock_shortcut.connect(self.on_unlock_shortcut)
        self.view.recents_requested.connect(self.on_recents_requested)
        self.view.snap_requested.connect(self.worker.snap_window)
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
            self, "Device Locked", "Enter PIN / password:",
            QLineEdit.EchoMode.Password)
        self.view.setFocus()
        return secret if ok and secret else None

    def on_pin_requested(self):
        self.worker.provide_pin(self._ask_secret())

    def on_unlock_shortcut(self):
        secret = self._ask_secret()
        if secret:
            self.worker.unlock_now(secret)

    def on_recents_requested(self):
        panel = self.view.recents_panel
        if panel.isVisible():
            panel.hide()
            return
        panel.show_items(None)          # "Loading..." while we query
        self.worker.request_recents()

    def on_task_chosen(self, comp):
        self.view.recents_panel.hide()
        self.worker.open_task(comp)
        self.view.setFocus()

    def on_screen_toggle(self, checked):
        self.worker.set_display_power(not checked)
        self.view.screen_btn.setText("Screen On" if checked else "Screen Off")

    def on_status(self, s):
        print(s)

    def closeEvent(self, e):
        self.worker.set_display_power(True)
        self.worker.stop()
        self.worker.wait(3000)
        super().closeEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())