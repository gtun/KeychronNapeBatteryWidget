"""System tray battery widget for the Keychron Nape Pro trackball."""

import argparse
import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QDir, QLockFile, QRectF, QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from nape_battery import Connection, Status, read_battery
from nape_bridge import BatteryBridge

LOW_BATTERY = 15
CRITICAL_BATTERY = 5

APP_NAME = "KeychronNapeBatteryWidget"

STATUS_TEXT = {
    Status.NOT_FOUND: "Nape Pro not detected (no cable or Link-KM receiver)",
    Status.ASLEEP: "Nape Pro asleep or out of range",
    Status.NO_PERMISSION: "No permission to open the device - install the udev rule",
    Status.ERROR: "Could not read battery",
}


def launch_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}"'
    if os.environ.get("APPIMAGE"):  # the AppImage's python lives in a temporary mount
        return f'"{os.environ["APPIMAGE"]}"'
    python = sys.executable
    if sys.platform == "win32":
        # pythonw has no console window
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw) if pythonw.exists() else python
    return f'"{python}" "{Path(__file__).resolve()}"'


class Autostart:
    """Start with the user session: HKCU Run key on Windows, XDG autostart elsewhere."""

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    DESKTOP_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / f"{APP_NAME}.desktop"

    @classmethod
    def enabled(cls) -> bool:
        if sys.platform == "win32":
            import winreg

            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as key:
                    winreg.QueryValueEx(key, APP_NAME)
                return True
            except OSError:
                return False
        return cls.DESKTOP_FILE.exists()

    @classmethod
    def set(cls, enable: bool):
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        elif enable:
            cls.DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
            cls.DESKTOP_FILE.write_text(
                "[Desktop Entry]\nType=Application\nName=Keychron Nape battery\n"
                f"Exec={launch_command()}\nX-GNOME-Autostart-enabled=true\n"
            )
        else:
            cls.DESKTOP_FILE.unlink(missing_ok=True)


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)

# (dark taskbar, light taskbar) variants, chosen for contrast on each.
GREEN = (QColor("#56d364"), QColor("#1a7f37"))
AMBER = (QColor("#e3b341"), QColor("#9a6700"))
RED = (QColor("#ff7b72"), QColor("#cf222e"))


def level_color(percent, charging, dark):
    pick = 0 if dark else 1
    if charging:
        return QColor(GREEN[pick])
    if percent is not None and percent <= LOW_BATTERY:
        return QColor(RED[pick])
    if percent is not None and percent <= 30:
        return QColor(AMBER[pick])
    return None


def draw_icon(percent, charging, foreground: QColor, dark=True, show_text=True) -> QIcon:
    """Draw the icon natively at every tray size, so small sizes stay crisp."""
    render = _render_number if show_text else _render_battery
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render(size, percent, charging, foreground, dark))
    return icon


def _with_alpha(color: QColor, alpha: int) -> QColor:
    color = QColor(color)
    color.setAlpha(alpha)
    return color


def _render_number(s, percent, charging, fg: QColor, dark) -> QPixmap:
    """The percentage as large as fits, above a thin level bar."""
    pixmap = QPixmap(s, s)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    alpha = fg.alpha()
    accent = level_color(percent, charging, dark)

    bar_h = max(3, round(s * 0.19))
    gap = max(1, round(s / 16))
    text_h = s - bar_h - gap
    bar = QRectF(0, s - bar_h, s, bar_h)
    radius = 0 if s < 24 else bar_h / 3
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_with_alpha(fg, min(alpha, 70)))
    p.drawRoundedRect(bar, radius, radius)
    if percent is not None:
        filled = QRectF(bar)
        filled.setWidth(max(1, round(s * percent / 100)))
        p.setBrush(_with_alpha(accent or fg, alpha))
        p.drawRoundedRect(filled, radius, radius)

    label = "?" if percent is None else str(percent)
    # Amber is only a hint, so it stays on the bar; red and green also tint the digits.
    text_color = accent if accent is not None and (charging or percent <= LOW_BATTERY) else fg
    font = QFont()
    font.setBold(True)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    # Largest font whose digits fit the text area height; squeeze wide labels
    # like "100" horizontally (down to 75%) before giving up height.
    for px in range(text_h * 2, 4, -1):
        font.setPixelSize(px)
        box = QFontMetrics(font).tightBoundingRect(label)
        squeeze = min(1.0, s / box.width())
        if box.height() <= text_h and squeeze >= 0.75:
            break
    p.setFont(font)
    p.setPen(_with_alpha(text_color, alpha))
    p.translate((s - box.width() * squeeze) / 2, 0)
    p.scale(squeeze, 1)
    baseline = round((text_h - box.height()) / 2 - box.top())
    p.drawText(-box.left(), baseline, label)
    p.end()
    return pixmap


def _render_battery(s, percent, charging, fg: QColor, dark) -> QPixmap:
    """Battery outline with its fill level, for --no-text."""
    pixmap = QPixmap(s, s)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    stroke = max(1.0, round(s / 12))
    body = QRectF(stroke / 2, s * 0.2 + stroke / 2, s * 0.86 - stroke, s * 0.6 - stroke)
    cap = QRectF(body.right() + stroke / 2, s * 0.36, s - body.right() - stroke / 2, s * 0.28)
    p.setPen(QPen(fg, stroke))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(body, stroke, stroke)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(fg)
    p.drawRect(cap)
    if percent is not None:
        inner = body.adjusted(stroke * 1.5, stroke * 1.5, -stroke * 1.5, -stroke * 1.5)
        inner.setWidth(max(1.0, inner.width() * percent / 100))
        p.setBrush(_with_alpha(level_color(percent, charging, dark) or fg, fg.alpha()))
        p.drawRect(inner)
    p.end()
    return pixmap


def draw_screen(percent: int, charging: bool, connection) -> QImage:
    """The picture for the 248x170 Vanguard keyboard screen, in its native RGB565."""
    from vanguard_screen import HEIGHT, WIDTH

    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    white, muted = QColor("#ffffff"), QColor(255, 255, 255, 140)
    accent = level_color(percent, charging, dark=True)
    digits = accent if accent is not None and (charging or percent <= LOW_BATTERY) else white

    pad = 14
    font = QFont("Segoe UI")
    font.setBold(True)
    font.setPixelSize(17)
    p.setFont(font)
    p.setPen(white)
    p.drawText(QRectF(pad, pad - 2, WIDTH - 2 * pad, 22), Qt.AlignmentFlag.AlignLeft, "NAPE PRO")
    if connection is not None:
        font.setBold(False)
        p.setFont(font)
        p.setPen(muted)
        label = "USB" if connection is Connection.WIRED else "2.4 GHz"
        p.drawText(QRectF(pad, pad - 2, WIDTH - 2 * pad, 22), Qt.AlignmentFlag.AlignRight, label)

    # Percentage, as large as fits between the header and the bar.
    number, unit = str(percent), "%"
    font.setBold(True)
    unit_font = QFont(font)
    bolt_w = 22 if charging else 0
    for size in range(84, 30, -2):  # shrink "100" and the charging bolt to fit the width
        font.setPixelSize(size)
        unit_font.setPixelSize(round(size * 0.45))
        box = QFontMetrics(font).tightBoundingRect(number)
        unit_width = QFontMetrics(unit_font).horizontalAdvance(unit)
        total = box.width() + 6 + unit_width + (10 + bolt_w if charging else 0)
        if total <= WIDTH - 2 * pad:
            break
    p.setFont(font)
    x = (WIDTH - total) / 2
    baseline = 112
    p.setPen(digits)
    p.drawText(round(x - box.left()), baseline, number)
    p.setFont(unit_font)
    p.setPen(muted)
    p.drawText(round(x + box.width() + 6), baseline, unit)
    if charging:
        bx, top, h = x + box.width() + 6 + unit_width + 10, baseline - 56, 56
        bolt = QPainterPath()
        bolt.moveTo(bx + 0.58 * bolt_w, top)
        for px, py in ((0, 0.55), (0.42, 0.55), (0.33, 1), (1, 0.4), (0.55, 0.4)):
            bolt.lineTo(bx + px * bolt_w, top + py * h)
        bolt.closeSubpath()
        p.fillPath(bolt, accent)

    bar = QRectF(pad, HEIGHT - pad - 14, WIDTH - 2 * pad, 14)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawRoundedRect(bar, 7, 7)
    filled = QRectF(bar)
    filled.setWidth(max(14.0, bar.width() * percent / 100))
    p.setBrush(accent or white)
    p.drawRoundedRect(filled, 7, 7)
    p.end()
    return image.convertToFormat(QImage.Format.Format_RGB16)


def rgb565_bytes(image: QImage) -> bytes:
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    data = bytes(ptr)
    row = image.width() * 2
    if image.bytesPerLine() == row:
        return data
    return b"".join(data[y * image.bytesPerLine():y * image.bytesPerLine() + row] for y in range(image.height()))


def taskbar_is_dark(app: QApplication, theme: str) -> bool:
    if theme != "auto":
        return theme == "dark"
    if sys.platform == "win32":
        # The taskbar follows the *system* theme, which can differ from the apps theme Qt reports.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 0
        except OSError:
            pass
    return app.styleHints().colorScheme() != Qt.ColorScheme.Light


class NapeTrayIcon(QSystemTrayIcon):
    def __init__(self, app: QApplication, interval_s: int, show_text: bool, theme: str = "auto",
                 vanguard: bool = False):
        super().__init__(app)
        self.app = app
        self.show_text = show_text
        self.theme = theme
        self.last_ok = None       # (BatteryReading, time) of the last successful read
        self.reading = None
        self.warned_low = False
        self.settings = QSettings("Keychron", APP_NAME)
        self.bridge = BatteryBridge()

        self.menu = QMenu()
        self.status_action = QAction("Reading...")
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.refresh_action = QAction("Refresh now")
        self.refresh_action.triggered.connect(self.update_status)
        self.menu.addAction(self.refresh_action)
        self.autostart_action = QAction("Start automatically at login")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(Autostart.enabled())
        self.autostart_action.toggled.connect(self.toggle_autostart)
        self.menu.addAction(self.autostart_action)
        self.vanguard_action = QAction("Show on Corsair Vanguard screen")
        self.vanguard_action.setCheckable(True)
        self.vanguard_action.setToolTip(f"Serve the battery level to the iCUE widget on localhost:{self.bridge.port}")
        self.vanguard_action.toggled.connect(self.toggle_vanguard)
        self.menu.addAction(self.vanguard_action)
        self.screen_action = QAction("Draw on Corsair Vanguard screen (no iCUE)")
        self.screen_action.setCheckable(True)
        self.screen_action.setToolTip("Overwrite one Web Hub screen image with the battery level")
        self.screen_action.toggled.connect(self.toggle_screen)
        self.menu.addAction(self.screen_action)
        self.quit_action = QAction("Quit")
        self.quit_action.triggered.connect(app.quit)
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        self.activated.connect(self.on_activated)

        app.styleHints().colorSchemeChanged.connect(lambda _scheme: self.render())

        self.timer = QTimer()
        self.timer.setInterval(interval_s * 1000)
        self.timer.timeout.connect(self.update_status)
        self.timer.start()
        app.aboutToQuit.connect(self.bridge.stop)

        self.update_status()
        self.setVisible(True)
        # Set after the first reading, so the widget never sees the "starting" placeholder for long.
        self.vanguard_action.setChecked(vanguard or self.settings.value("vanguard", False, type=bool))
        self.screen_action.blockSignals(True)
        self.screen_action.setChecked(self.settings.value("screen/enabled", False, type=bool))
        self.screen_action.blockSignals(False)
        self.draw_screen_if_needed()

    def toggle_screen(self, enable):
        self.settings.setValue("screen/enabled", enable)
        if not enable:
            return
        from vanguard_screen import ScreenError, VanguardScreen

        try:
            with VanguardScreen.open() as screen:
                images = screen.screen_images()
        except Exception as exc:  # hidapi errors, ScreenError, missing keyboard
            self.screen_failed(f"Could not reach the keyboard screen: {exc}")
            return
        if not images:
            self.screen_failed("The keyboard has no screen image to draw over. Upload any image "
                               "to its screen with Corsair Web Hub (Screen tab), then try again.")
            return
        # Draw over the newest image; the others stay as they are.
        self.settings.setValue("screen/image", images[-1])
        self.settings.remove("screen/last")
        self.draw_screen_if_needed()

    def screen_failed(self, message):
        self.showMessage("Keychron Nape Pro", message, QSystemTrayIcon.MessageIcon.Warning)
        self.screen_action.blockSignals(True)
        self.screen_action.setChecked(False)
        self.screen_action.blockSignals(False)
        self.settings.setValue("screen/enabled", False)

    def draw_screen_if_needed(self):
        """Redraw the keyboard screen when the battery level changed enough to matter."""
        if not self.screen_action.isChecked() or self.reading is None or self.reading.status is not Status.OK:
            return  # keep the last picture while the Nape sleeps, rather than writing flash
        from vanguard_screen import VanguardScreen, should_redraw

        r = self.reading
        last = self.settings.value("screen/last")
        last = tuple(int(v) for v in str(last).split(",")) if last else None
        if not should_redraw(None if last is None else (last[0], bool(last[1])), r.percent, r.charging):
            return
        image_id = self.settings.value("screen/image", type=int)
        try:
            with VanguardScreen.open() as screen:
                screen.replace_image(image_id, rgb565_bytes(draw_screen(r.percent, r.charging, r.connection)))
        except Exception:
            return  # unplugged or busy: try again on the next reading, without nagging
        self.settings.setValue("screen/last", f"{r.percent},{int(r.charging)}")

    def toggle_vanguard(self, enable):
        if enable:
            try:
                self.bridge.start()
            except OSError as exc:
                self.showMessage("Keychron Nape Pro",
                                 f"Could not serve the battery level on port {self.bridge.port}: {exc}",
                                 QSystemTrayIcon.MessageIcon.Warning)
                self.vanguard_action.blockSignals(True)
                self.vanguard_action.setChecked(False)
                self.vanguard_action.blockSignals(False)
                return
            if self.reading is not None:
                self.bridge.publish(self.reading, self.last_ok)
        else:
            self.bridge.stop()
        self.settings.setValue("vanguard", enable)

    def toggle_autostart(self, enable):
        try:
            Autostart.set(enable)
        except OSError as exc:
            self.showMessage("Keychron Nape Pro", f"Could not change autostart: {exc}",
                             QSystemTrayIcon.MessageIcon.Warning)
        self.autostart_action.blockSignals(True)
        self.autostart_action.setChecked(Autostart.enabled())
        self.autostart_action.blockSignals(False)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.update_status()

    def dark(self) -> bool:
        return taskbar_is_dark(self.app, self.theme)

    def foreground(self) -> QColor:
        return QColor("#ffffff") if self.dark() else QColor("#1f2328")

    def update_status(self):
        self.reading = read_battery()
        if self.reading.status is Status.OK:
            self.last_ok = (self.reading, time.time())
            self.notify_if_low(self.reading)
        self.bridge.publish(self.reading, self.last_ok)
        self.render()
        self.draw_screen_if_needed()

    def notify_if_low(self, reading):
        if reading.charging or reading.percent > LOW_BATTERY:
            self.warned_low = False
            return
        if not self.warned_low:
            self.warned_low = True
            icon = (QSystemTrayIcon.MessageIcon.Critical if reading.percent <= CRITICAL_BATTERY
                    else QSystemTrayIcon.MessageIcon.Warning)
            self.showMessage("Keychron Nape Pro", f"Battery low: {reading.percent}%", icon)

    def render(self):
        r = self.reading
        if r.status is Status.OK:
            mode = "USB cable" if r.connection is Connection.WIRED else "2.4 GHz"
            text = f"Nape Pro battery: {r.percent}%"
            if r.charging:
                text += " (charging)"
            text += f" - {mode}"
            self.setIcon(draw_icon(r.percent, r.charging, self.foreground(), self.dark(), self.show_text))
        else:
            text = STATUS_TEXT[r.status]
            if r.detail:
                text += f"\n{r.detail}"
            if self.last_ok:
                # Keep showing the last known level while the Nape sleeps, but faded.
                last, when = self.last_ok
                text += f"\nLast reading: {last.percent}% at {time.strftime('%H:%M', time.localtime(when))}"
                color = self.foreground()
                color.setAlpha(140)
                self.setIcon(draw_icon(last.percent, False, color, self.dark(), self.show_text))
            else:
                self.setIcon(draw_icon(None, False, self.foreground(), self.dark(), self.show_text))
        self.setToolTip(text)
        self.status_action.setText(text.splitlines()[0])


def main():
    parser = argparse.ArgumentParser(description="Keychron Nape Pro battery tray widget")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between battery reads (default: 300)")
    parser.add_argument("--no-text", action="store_true",
                        help="draw a battery outline instead of the percentage number")
    parser.add_argument("--theme", choices=("auto", "dark", "light"), default="auto",
                        help="icon colours for a dark or light taskbar/panel (default: auto)")
    parser.add_argument("--vanguard", action="store_true",
                        help="feed the iCUE widget on a Corsair Vanguard keyboard screen "
                             "(same as the tray menu option, which is remembered)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # Only one tray icon, even if autostart and a manual launch overlap.
    lock = QLockFile(QDir.temp().filePath(f"{APP_NAME}.lock"))
    if not lock.tryLock(100):
        print("Already running", file=sys.stderr)
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available on this desktop", file=sys.stderr)
        return 1
    tray = NapeTrayIcon(app, max(args.interval, 10), not args.no_text, args.theme, args.vanguard)
    tray.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
