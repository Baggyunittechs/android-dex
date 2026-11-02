# stream.py  (Phase 2: touch, keyboard, scroll, nav buttons, screen blanking)
import random
import socket
import struct
import sys
import threading
import time

import av
from adbutils import adb
from PyQt6.QtCore import QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QMainWindow, QToolBar, QWidget

SERVER_JAR = "scrcpy-server-v3.1"
SERVER_VERSION = "3.1"
REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
LOCAL_PORT = 27183
VIDEO_BITRATE = 16_000_000      # was 8M; raise/lower to taste
SCROLL_SIGN = 1                 # set to -1 if the wheel scrolls the wrong way

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


class StreamWorker(QThread):
    frame_ready = pyqtSignal(QImage)
    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._shell = None
        self.control_sock = None
        self.frame_w = 0
        self.frame_h = 0
        self.display_off = False
        self._lock = threading.Lock()

    def stop(self):
        self._running = False

    # ---------- server / sockets ----------
    def _start_server(self, d, scid):
        d.sync.push(SERVER_JAR, REMOTE_JAR)
        d.forward(f"tcp:{LOCAL_PORT}", f"localabstract:scrcpy_{scid:08x}")
        cmd = (
            f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server "
            f"{SERVER_VERSION} scid={scid:08x} log_level=info tunnel_forward=true "
            f"audio=false control=true max_size=0 video_bit_rate={VIDEO_BITRATE} "
            "max_fps=60 send_device_meta=false send_codec_meta=false "
            "send_frame_meta=false"
        )
        self._shell = d.shell(cmd, stream=True)
        conn = self._shell

        def drain():
            try:
                while True:
                    chunk = conn.read(1024)
                    if not chunk:
                        print("[server] output stream ended")
                        break
                    print("[server]", chunk.decode(errors="ignore").strip())
            except Exception as e:
                print("[server] drain error:", repr(e))

        threading.Thread(target=drain, daemon=True).start()

    def _connect_video(self):
        for _ in range(50):
            s = socket.socket()
            try:
                s.connect(("127.0.0.1", LOCAL_PORT))
                if s.recv(1):                 # dummy byte, first connection only
                    return s
            except OSError:
                pass
            s.close()
            time.sleep(0.1)
        raise RuntimeError("Could not connect video socket")

    def _connect_control(self):
        s = socket.socket()
        s.connect(("127.0.0.1", LOCAL_PORT))   # second connection = control

        def drain_control():
            try:
                while s.recv(4096):
                    pass
            except OSError:
                pass

        threading.Thread(target=drain_control, daemon=True).start()
        return s

    # ---------- control messages ----------
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

    # ---------- main loop ----------
    def run(self):
        d = None
        sock = None
        try:
            devices = adb.device_list()
            if not devices:
                self.status.emit("No device found")
                return
            d = devices[0]
            d.shell("input keyevent KEYCODE_WAKEUP")
            self._start_server(d, random.randint(1, 0x7FFFFFFF))
            sock = self._connect_video()        # must be first
            self.control_sock = self._connect_control()
            sock.settimeout(1.0)
            self.status.emit("Connected (video + control)")

            codec = av.CodecContext.create("h264", "r")
            frames = 0
            start = time.time()
            last = start

            while self._running:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as e:
                    self.status.emit(f"Socket error: {e}")
                    break
                if not data:
                    self.status.emit(f"Stream closed after {time.time()-start:.1f}s")
                    break

                for packet in codec.parse(data):
                    try:
                        decoded = codec.decode(packet)
                    except av.error.InvalidDataError:
                        continue
                    for frame in decoded:
                        frames += 1
                        arr = frame.to_ndarray(format="rgb24")
                        h, w, _ = arr.shape
                        self.frame_w, self.frame_h = w, h
                        img = QImage(arr.data, w, h, 3 * w,
                                     QImage.Format.Format_RGB888).copy()
                        self.frame_ready.emit(img)

                if time.time() - last > 1:
                    self.status.emit(f"{self.frame_w}x{self.frame_h}  frames={frames}")
                    last = time.time()
        except Exception as e:
            self.status.emit(f"Error: {e!r}")
        finally:
            if self.display_off:                 # never leave the phone dark
                self.set_display_power(True)
            for s in (sock, self.control_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            self.control_sock = None
            if d:
                try:
                    d.forward_remove(f"tcp:{LOCAL_PORT}")
                except Exception:
                    pass


class VideoWidget(QWidget):
    touch = pyqtSignal(int, float, float)      # action, nx, ny
    scroll = pyqtSignal(float, float, float)   # nx, ny, vscroll
    key = pyqtSignal(int)                      # android keycode
    text = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.image = None
        self._pressed = False

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

    # --- mouse ---
    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() != Qt.MouseButton.LeftButton:
            return
        n = self._normalize(e.position(), clamp=False)
        if n:
            self._pressed = True
            self.touch.emit(ACTION_DOWN, *n)

    def mouseMoveEvent(self, e):
        if self._pressed:
            n = self._normalize(e.position(), clamp=True)
            if n:
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

    # --- keyboard ---
    def keyPressEvent(self, e):
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                            Qt.KeyboardModifier.AltModifier):
            return super().keyPressEvent(e)       # let window shortcuts work
        code = SPECIAL_KEYS.get(e.key())
        if code is not None:
            self.key.emit(code)
        elif e.text() and e.text().isprintable():
            self.text.emit(e.text())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Custom Dex")
        self.resize(420, 900)
        self.view = VideoWidget()
        self.setCentralWidget(self.view)

        self.worker = StreamWorker()
        self.worker.frame_ready.connect(self.view.set_image)
        self.worker.status.connect(self.on_status)
        self.view.touch.connect(self.worker.send_touch)
        self.view.scroll.connect(self.worker.send_scroll)
        self.view.key.connect(self.worker.send_keycode)
        self.view.text.connect(self.worker.send_text)

        self._build_toolbar()
        self.worker.start()
        self.view.setFocus()

    def _build_toolbar(self):
        tb = QToolBar("Navigation")
        tb.setMovable(False)
        tb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, tb)

        def add(text, shortcut, fn, checkable=False):
            a = QAction(text, self)
            a.setShortcut(shortcut)
            a.setCheckable(checkable)
            a.setToolTip(f"{text} ({shortcut})")
            a.triggered.connect(fn)
            tb.addAction(a)
            return a

        add("Back", "Ctrl+B", lambda: self.worker.send_keycode(KEY_BACK))
        add("Home", "Ctrl+H", lambda: self.worker.send_keycode(KEY_HOME))
        add("Recents", "Ctrl+R", lambda: self.worker.send_keycode(KEY_APP_SWITCH))
        tb.addSeparator()
        add("Screen off", "Ctrl+P",
            lambda checked: self.worker.set_display_power(not checked),
            checkable=True)

    def on_status(self, s):
        print(s)
        self.statusBar().showMessage(s)

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