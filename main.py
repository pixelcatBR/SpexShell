#!/usr/bin/env python3
"""
spex-shell — desktop feito de shell, iniciado do shell.
Estética hacker/TTY: verde-fósforo, glow, mono.

- Fullscreen, fundo preto, sem chrome, sem mouse.
- N shells reais (PTY + fork) em tiling: master-stack, colunas, linhas, grid.
- Teclado 100%: prefixo Ctrl+B (estilo tmux).
- Fonte "hacker de filme" auto-detectada (VT323, Fixedsys, IBM VGA, ...).
- Glow verde no texto, cursor âmbar, scanline CRT no cabeçalho.

Uso:
    spex-shell
    spex-shell --shell /bin/zsh --font VT323 --size 18
    spex-shell --args -l

Prefix = Ctrl+B:
  c novo painel        q fechar painel (renasce se for o último)
  Q ou x desligar o SISTEMA (com confirmação)
  h/j/k/l foco         n/p ciclo        t layout
  z zoom               r renomear

Env vars:
  SPEX_SHELL   shell padrão
  SPEX_FONT    fonte
  SPEX_SIZE    tamanho da fonte
"""
import os
import sys
import pty
import fcntl
import signal
import struct
import termios
import argparse
import subprocess

from PyQt6.QtCore import Qt, QTimer, QSocketNotifier, QRect
from PyQt6.QtGui import (
    QFont, QFontMetrics, QColor, QPainter, QKeyEvent, QPen, QFontDatabase,
)
from PyQt6.QtWidgets import QApplication, QWidget, QInputDialog

APP_NAME = "spex-shell"

# ---------------------------------------------------------------------------
# Fonte "hacker de filme"
# ---------------------------------------------------------------------------
HACKER_FONT_CANDIDATES = [
    "VT323",
    "Fixedsys Excelsior",
    "Fixedsys",
    "IBM VGA 8x16",
    "Px437 IBM VGA8",
    "Terminus",
    "Liberation Mono",
    "DejaVu Sans Mono",
    "Courier New",
    "Courier",
    "monospace",
]

def pick_hacker_font(preferred: str | None) -> str:
    if preferred:
        return preferred
    fams = set(QFontDatabase.families())
    for name in HACKER_FONT_CANDIDATES:
        if name in fams:
            return name
    return "monospace"

# Paleta hacker
COL_BG         = QColor(0,   0,   0)
COL_FG         = QColor(60, 255, 120)
COL_FG_DIM     = QColor(30, 120, 60)
COL_GLOW       = QColor(40, 255, 90, 90)
COL_CURSOR     = QColor(255, 190, 60)
COL_HEADER_BG  = QColor(0,   18,  6)
COL_HEADER_FG  = QColor(120, 255, 150)
COL_BORDER_ON  = QColor(60, 255, 120)
COL_BORDER_OFF = QColor(0,   45,  20)
COL_SEP        = QColor(0,   35,  15)
COL_WARN       = QColor(255, 80, 80)
COL_PROMPT_BG  = QColor(40, 0, 0)

SAFE_TOP    = 6
SAFE_BOTTOM = 4
SAFE_SIDE   = 4

CONFIRM_TIMEOUT_MS = 5000   # janela de confirmação em ms


# ---------------------------------------------------------------------------
# Desligar o sistema (com fallbacks)
# ---------------------------------------------------------------------------
def shutdown_system():
    """Tenta desligar o sistema por vários caminhos. Retorna (ok, cmd)."""
    candidates = [
        ["systemctl", "poweroff"],
        ["loginctl", "poweroff"],
        ["shutdown", "-h", "now"],
        ["poweroff"],
        ["halt", "-p"],
    ]
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3)
            if r.returncode == 0:
                return True, " ".join(cmd)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False, ""


# ---------------------------------------------------------------------------
# PTY real (sem emulador)
# ---------------------------------------------------------------------------
class Pty:
    def __init__(self, shell, args, rows, cols):
        self.rows, self.cols = rows, cols
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ["COLORTERM"] = "truecolor"
                os.execvp(shell, [shell] + args)
            except Exception:
                os._exit(1)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, os.O_NONBLOCK)
        self.resize(rows, cols)

    def resize(self, rows, cols):
        self.rows, self.cols = max(1, rows), max(1, cols)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def read(self):
        try:
            return os.read(self.fd, 65536)
        except (BlockingIOError, OSError):
            return b""

    def write(self, data):
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def close(self):
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass


# ---------------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------------
class Pane(QWidget):
    PAD    = 4
    HEADER = 13

    def __init__(self, wm, shell, args, index):
        super().__init__(wm)
        self.wm = wm
        self.index = index
        self.title = f"shell-{index}"

        self._font = QFont(wm.font_family, wm.font_size)
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        self._font.setFixedPitch(True)
        self._fm = QFontMetrics(self._font)
        self._cw = max(1, self._fm.horizontalAdvance("M"))
        self._ch = max(1, self._fm.height())

        self.rows, self.cols = 24, 80
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.cx = self.cy = 0

        self.pty = Pty(shell, args, self.rows, self.cols)
        self.notifier = QSocketNotifier(self.pty.fd, QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._on_readable)

        self._cursor_on = True
        self._blink = QTimer(self)
        self._blink.timeout.connect(self._toggle_cursor)
        self._blink.start(530)

        self.active = False

    def _on_readable(self):
        data = self.pty.read()
        if data:
            self._feed(data)
            self.update()

    def send_bytes(self, data):
        self.pty.write(data)

    def _feed(self, data: bytes):
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == 0x1B:
                i = self._skip_ansi(data, i)
                continue
            if b == 0x0D:
                self.cx = 0
            elif b == 0x0A:
                self._newline()
            elif b == 0x08:
                self.cx = max(0, self.cx - 1)
            elif b == 0x09:
                self.cx = min(self.cols - 1, (self.cx // 8 + 1) * 8)
            elif b == 0x07:
                pass
            else:
                if self.cx >= self.cols:
                    self.cx = 0
                    self._newline()
                if 0x20 <= b < 0x7F:
                    self.grid[self.cy][self.cx] = chr(b)
                elif b >= 0x80:
                    self.grid[self.cy][self.cx] = "·"
                self.cx += 1
            i += 1

    def _skip_ansi(self, data, i):
        n = len(data)
        if i + 1 >= n:
            return n
        nxt = data[i + 1]
        if nxt == 0x5B:
            j = i + 2
            while j < n and not (0x40 <= data[j] <= 0x7E):
                j += 1
            return j + 1
        if nxt == 0x5D:
            j = i + 2
            while j < n and data[j] != 0x07:
                j += 1
            return j + 1
        return i + 2

    def _newline(self):
        self.cy += 1
        if self.cy >= self.rows:
            self.cy = self.rows - 1
            self.grid.pop(0)
            self.grid.append([" "] * self.cols)

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        self.update()

    def apply_geometry(self, rect: QRect):
        self.setGeometry(rect)
        avail_w = max(1, rect.width() - 2 * self.PAD)
        avail_h = max(1, rect.height() - 2 * self.PAD - self.HEADER)
        cols = max(1, avail_w // self._cw)
        rows = max(1, avail_h // self._ch)
        if (rows, cols) != (self.rows, self.cols):
            self.rows, self.cols = rows, cols
            new_grid = [[" "] * cols for _ in range(rows)]
            for y in range(min(rows, len(self.grid))):
                for x in range(min(cols, len(self.grid[y]))):
                    new_grid[y][x] = self.grid[y][x]
            self.grid = new_grid
            self.cx = min(self.cx, cols - 1)
            self.cy = min(self.cy, rows - 1)
            self.pty.resize(rows, cols)

    def _draw_glow_text(self, p: QPainter, x: int, y: int, text: str,
                        color: QColor, glow: QColor):
        p.setPen(glow)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(x + dx, y + dy, text)
        p.setPen(color)
        p.drawText(x, y, text)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), COL_BG)
        p.setFont(self._font)

        header_rect = QRect(0, 0, self.width(), self.HEADER + self.PAD)
        p.fillRect(header_rect, COL_HEADER_BG)
        title = f"[{self.index}] {self.title}"
        if self.active:
            self._draw_glow_text(p, self.PAD, self.HEADER - 2, title,
                                 COL_HEADER_FG, COL_GLOW)
        else:
            p.setPen(COL_FG_DIM)
            p.drawText(self.PAD, self.HEADER - 2, title)

        p.setPen(QPen(COL_SEP, 1))
        p.drawLine(0, self.HEADER + self.PAD, self.width(), self.HEADER + self.PAD)

        y0 = self.HEADER + 2 * self.PAD
        fg = COL_FG if self.active else COL_FG_DIM
        for y, row in enumerate(self.grid):
            text = "".join(row).rstrip()
            if not text:
                continue
            baseline = y0 + (y + 1) * self._ch - self._fm.descent()
            if self.active:
                self._draw_glow_text(p, self.PAD, baseline, text, fg, COL_GLOW)
            else:
                p.setPen(fg)
                p.drawText(self.PAD, baseline, text)

        if self._cursor_on and self.active:
            r = QRect(
                self.PAD + self.cx * self._cw,
                y0 + self.cy * self._ch,
                self._cw,
                self._ch,
            )
            p.fillRect(r, COL_CURSOR)
            ch = self.grid[self.cy][self.cx]
            if ch != " ":
                p.setPen(QColor(0, 0, 0))
                p.drawText(r.left() + 1, r.bottom() - self._fm.descent(), ch)

        p.setPen(QPen(COL_BORDER_ON if self.active else COL_BORDER_OFF, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))


# ---------------------------------------------------------------------------
# SpexShell
# ---------------------------------------------------------------------------
class SpexShell(QWidget):
    def __init__(self, shell, shell_args, font_family, font_size):
        super().__init__()
        self.shell = shell
        self.shell_args = shell_args
        self.font_family = font_family
        self.font_size = font_size

        self.setWindowTitle(APP_NAME)
        self.setStyleSheet("background:#000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.panes = []
        self.focus = 0
        self.layout_name = "master-stack"
        self.layouts = ("master-stack", "columns", "rows", "grid")
        self.prefix_pressed = False
        self.zoomed = False

        # ---- estado do prompt de desligar ----
        self.confirm_shutdown = False
        self.confirm_timer = QTimer(self)
        self.confirm_timer.setSingleShot(True)
        self.confirm_timer.timeout.connect(self._cancel_shutdown)

        # ---- status line ----
        self.status_msg = ""
        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)
        self.status_timer.timeout.connect(self._clear_status)

        self.showFullScreen()
        QTimer.singleShot(0, self._fix_fullscreen_geometry)
        self.add_pane()

    def _fix_fullscreen_geometry(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())
            self.showFullScreen()
            self.raise_()
            self.activateWindow()
        self.apply_layout()

    # ---------- status ----------
    def _set_status(self, msg, ms=2000):
        self.status_msg = msg
        self.status_timer.start(ms)
        self.update()

    def _clear_status(self):
        self.status_msg = ""
        self.update()

    # ---------- painéis ----------
    def add_pane(self):
        idx = len(self.panes) + 1
        pane = Pane(self, self.shell, self.shell_args, idx)
        pane.show()
        self.panes.append(pane)
        self.focus = len(self.panes) - 1
        self._update_active()
        self.apply_layout()

    def remove_pane(self, pane):
        if pane not in self.panes:
            return
        idx = self.panes.index(pane)
        self.panes.pop(idx)
        pane.pty.close()
        pane.close()
        pane.deleteLater()

        if not self.panes:
            # não desliga o app: renasce um painel
            self.focus = 0
            self.add_pane()
            return

        self.focus = min(idx, len(self.panes) - 1)
        self._update_active()
        self.apply_layout()

    def _update_active(self):
        for i, p in enumerate(self.panes):
            p.active = (i == self.focus)
            p.update()

    def _move_focus(self, d):
        if not self.panes:
            return
        self.focus = (self.focus + d) % len(self.panes)
        self._update_active()

    # ---------- geometria / layout ----------
    def _usable_rect(self):
        # desconta espaço para status line quando ela está ativa
        bottom = SAFE_BOTTOM
        if self.status_msg or self.confirm_shutdown:
            bottom = max(bottom, 22)
        return QRect(
            SAFE_SIDE,
            SAFE_TOP,
            max(1, self.width() - 2 * SAFE_SIDE),
            max(1, self.height() - SAFE_TOP - bottom),
        )

    def apply_layout(self):
        n = len(self.panes)
        if n == 0:
            return
        area = self._usable_rect()
        if self.zoomed and n > 1:
            for i, p in enumerate(self.panes):
                if i == self.focus:
                    p.apply_geometry(area)
                    p.show()
                else:
                    p.hide()
            return
        for p in self.panes:
            p.show()
        rects = self._compute_rects(n, area, self.layout_name)
        for p, r in zip(self.panes, rects):
            p.apply_geometry(r)

    def _compute_rects(self, n, area: QRect, kind):
        W, H = area.width(), area.height()
        X, Y = area.x(), area.y()
        if n == 1:
            return [QRect(X, Y, W, H)]
        if kind == "columns":
            w = W // n
            out = []
            for i in range(n):
                x = X + i * w
                ww = (X + W - x) if i == n - 1 else w
                out.append(QRect(x, Y, ww, H))
            return out
        if kind == "rows":
            h = H // n
            out = []
            for i in range(n):
                y = Y + i * h
                hh = (Y + H - y) if i == n - 1 else h
                out.append(QRect(X, y, W, hh))
            return out
        if kind == "grid":
            import math
            cols = int(math.ceil(math.sqrt(n)))
            rows = int(math.ceil(n / cols))
            cw = W // cols
            ch = H // rows
            out = []
            for i in range(n):
                r, c = divmod(i, cols)
                x = X + c * cw
                y = Y + r * ch
                ww = (X + W - x) if c == cols - 1 else cw
                hh = (Y + H - y) if r == rows - 1 else ch
                out.append(QRect(x, y, ww, hh))
            return out
        # master-stack
        if n == 2:
            w = W // 2
            return [QRect(X, Y, w, H), QRect(X + w, Y, W - w, H)]
        mw = W // 2
        sw = W - mw
        out = [QRect(X, Y, mw, H)]
        sh = H // (n - 1)
        for i in range(n - 1):
            y = Y + i * sh
            hh = (Y + H - y) if i == n - 2 else sh
            out.append(QRect(X + mw, y, sw, hh))
        return out

    def resizeEvent(self, ev):
        self.apply_layout()

    # ---------- desligar sistema ----------
    def _request_shutdown(self):
        self.confirm_shutdown = True
        self.confirm_timer.start(CONFIRM_TIMEOUT_MS)
        self.apply_layout()
        self._set_status("desligar o sistema? [y/N]", CONFIRM_TIMEOUT_MS + 200)
        self.update()

    def _cancel_shutdown(self):
        if not self.confirm_shutdown:
            return
        self.confirm_shutdown = False
        self._set_status("desligamento cancelado", 1200)
        self.apply_layout()

    def _do_shutdown(self):
        self.confirm_shutdown = False
        self.confirm_timer.stop()
        self._set_status("desligando o sistema...", 10000)
        self.apply_layout()

        # fecha PTYs primeiro (evita orfãos reclamando)
        for p in list(self.panes):
            try:
                p.pty.close()
            except Exception:
                pass

        ok, cmd = shutdown_system()
        if not ok:
            # sem permissão / sem systemd / etc
            self._set_status(
                "falha ao desligar (sem systemctl/loginctl/poweroff). "
                "rode: sudo poweroff",
                8000,
            )
            # renasce um painel para não deixar a tela vazia
            if not self.panes:
                self.add_pane()
            return

        self._set_status(f"ok: {cmd}", 5000)
        QTimer.singleShot(400, lambda: QApplication.instance().quit())

    # ---------- teclado ----------
    def keyPressEvent(self, ev: QKeyEvent):
        # ---- se está no prompt de confirmação ----
        if self.confirm_shutdown:
            t = ev.text().lower()
            if t == "y":
                self._do_shutdown()
                return
            # qualquer outra tecla cancela
            self._cancel_shutdown()
            return

        if self.prefix_pressed:
            self.prefix_pressed = False
            self._handle_prefix(ev)
            return

        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if ctrl and ev.key() == Qt.Key.Key_B:
            self.prefix_pressed = True
            return

        data = self._key_to_bytes(ev)
        if data and self.panes:
            self.panes[self.focus].send_bytes(data)

    def _handle_prefix(self, ev):
        k = ev.key()
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        # ---- desligar o SISTEMA ----
        if (k == Qt.Key.Key_Q and shift) or k == Qt.Key.Key_X:
            self._request_shutdown()
            return

        # ---- fechar painel focado ----
        if k == Qt.Key.Key_Q and not shift:
            if self.panes:
                self.remove_pane(self.panes[self.focus])
            return

        # ---- resto ----
        if k == Qt.Key.Key_C:
            self.add_pane()
        elif k in (Qt.Key.Key_H, Qt.Key.Key_K):
            self._move_focus(-1)
        elif k in (Qt.Key.Key_L, Qt.Key.Key_J, Qt.Key.Key_N):
            self._move_focus(+1)
        elif k == Qt.Key.Key_P:
            self._move_focus(-1)
        elif k == Qt.Key.Key_T:
            i = self.layouts.index(self.layout_name)
            self.layout_name = self.layouts[(i + 1) % len(self.layouts)]
            self.apply_layout()
        elif k == Qt.Key.Key_Z:
            self.zoomed = not self.zoomed
            self.apply_layout()
        elif k == Qt.Key.Key_R and self.panes:
            cur = self.panes[self.focus].title
            text, ok = QInputDialog.getText(self, "renomear", "novo nome:", text=cur)
            if ok and text:
                self.panes[self.focus].title = text
                self.panes[self.focus].update()

    def _key_to_bytes(self, ev):
        key = ev.key()
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        specials = {
            Qt.Key.Key_Return: b"\r", Qt.Key.Key_Enter: b"\r",
            Qt.Key.Key_Backspace: b"\x7f", Qt.Key.Key_Tab: b"\t",
            Qt.Key.Key_Escape: b"\x1b",
            Qt.Key.Key_Up: b"\x1b[A", Qt.Key.Key_Down: b"\x1b[B",
            Qt.Key.Key_Right: b"\x1b[C", Qt.Key.Key_Left: b"\x1b[D",
            Qt.Key.Key_Home: b"\x1b[H", Qt.Key.Key_End: b"\x1b[F",
            Qt.Key.Key_PageUp: b"\x1b[5~", Qt.Key.Key_PageDown: b"\x1b[6~",
            Qt.Key.Key_Insert: b"\x1b[2~", Qt.Key.Key_Delete: b"\x1b[3~",
            Qt.Key.Key_F1: b"\x1bOP", Qt.Key.Key_F2: b"\x1bOQ",
            Qt.Key.Key_F3: b"\x1bOR", Qt.Key.Key_F4: b"\x1bOS",
            Qt.Key.Key_F5: b"\x1b[15~", Qt.Key.Key_F6: b"\x1b[17~",
            Qt.Key.Key_F7: b"\x1b[18~", Qt.Key.Key_F8: b"\x1b[19~",
            Qt.Key.Key_F9: b"\x1b[20~", Qt.Key.Key_F10: b"\x1b[21~",
            Qt.Key.Key_F11: b"\x1b[23~", Qt.Key.Key_F12: b"\x1b[24~",
        }
        if key in specials:
            return specials[key]
        text = ev.text()
        if ctrl and text and text.isalpha():
            return bytes([ord(text.lower()) - ord('a') + 1])
        if text:
            return text.encode("utf-8")
        return b""

    # ---------- status line / prompt ----------
    def paintEvent(self, ev):
        super().paintEvent(ev)
        # se nada a mostrar, não pinta barra
        if not self.status_msg and not self.confirm_shutdown:
            return
        p = QPainter(self)
        p.setFont(self.panes[0]._font if self.panes else QFont("monospace", 12))
        fm = QFontMetrics(p.font())
        h = fm.height() + 6
        bar = QRect(0, self.height() - h, self.width(), h)
        p.fillRect(bar, COL_PROMPT_BG if self.confirm_shutdown else COL_HEADER_BG)
        p.setPen(QPen(COL_WARN if self.confirm_shutdown else COL_HEADER_FG, 1))
        p.drawLine(bar.left(), bar.top(), bar.right(), bar.top())

        msg = self.status_msg or (
            "desligar o sistema? [y/N]" if self.confirm_shutdown else ""
        )
        p.setPen(COL_WARN if self.confirm_shutdown else COL_HEADER_FG)
        p.drawText(bar.left() + 8, bar.bottom() - fm.descent() - 2, msg)

    # ---------- fechar ----------
    def closeEvent(self, ev):
        for p in list(self.panes):
            try:
                p.pty.close()
            except Exception:
                pass
        self.panes.clear()
        super().closeEvent(ev)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--shell",
                    default=os.environ.get("SPEX_SHELL",
                                           os.environ.get("SHELL", "/bin/sh")))
    ap.add_argument("--args", nargs=argparse.REMAINDER, default=[])
    ap.add_argument("--font", default=os.environ.get("SPEX_FONT", None),
                    help="força uma fonte; default = auto-detecta hacker")
    ap.add_argument("--size", type=int,
                    default=int(os.environ.get("SPEX_SIZE", "15")))
    ns = ap.parse_args()

    shell_args = ns.args or (["-l"] if os.path.basename(ns.shell) in
                             ("bash", "zsh", "fish") else [])

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    font = pick_hacker_font(ns.font)
    wm = SpexShell(ns.shell, shell_args, font, ns.size)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())