"""Interactive terminal widget.

A ``QTextEdit`` driven by :class:`~ciscoiosbox.ui.ansi.AnsiParser`. Keystrokes
are captured, translated to the bytes a Cisco device expects, and emitted for
the connection worker to send; the device's echo is what actually appears on
screen (no local echo), exactly as PuTTY behaves.

Rendering model: the document is treated as a scrollback log with an editable
last line. ``\\r`` moves to the start of the current line and subsequent text
*overwrites* rather than inserts, which is what makes IOS's ``--More--``
erasure and command-line editing render correctly.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFontMetricsF, QKeyEvent, QTextCharFormat, QTextCursor, QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QMenu, QTextEdit

from .ansi import AnsiEvent, AnsiParser, EventType, strip_ansi
from .theme import Palette, monospace_font

log = logging.getLogger(__name__)


class TerminalWidget(QTextEdit):
    """VT-ish terminal emulator sufficient for the Cisco IOS CLI."""

    #: Keystrokes (already encoded as the characters to transmit).
    data_entered = Signal(str)
    #: Emitted when the visible geometry changes, so we can resize the remote pty.
    resized = Signal(int, int)          # (columns, rows)

    #: Cap on retained lines. Beyond this the oldest are dropped — an unbounded
    #: document turns a long `show tech-support` into an out-of-memory crash.
    DEFAULT_SCROLLBACK = 5000

    def __init__(self, parent=None, scrollback: int = DEFAULT_SCROLLBACK) -> None:
        super().__init__(parent)

        self._parser = AnsiParser()
        self._scrollback = scrollback
        self._connected = False
        self._local_echo = False
        self._font_size = 11

        # Batch incoming data: a chatty device can emit dozens of small reads per
        # second, and re-rendering on each one is what makes naive terminals crawl.
        self._pending_events: list[AnsiEvent] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(16)          # ~60 fps
        self._flush_timer.timeout.connect(self._flush)

        self._configure_appearance()

        self.setReadOnly(True)                     # typing is intercepted, not inserted
        self.setUndoRedoEnabled(False)
        self.setAcceptRichText(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── appearance ────────────────────────────────────────────────────────────

    def _configure_appearance(self) -> None:
        font = monospace_font(self._font_size)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(f"""
            QTextEdit {{
                background-color: #0d1117;
                color: {Palette.TEXT};
                border: 1px solid {Palette.BORDER};
                border-radius: 6px;
                padding: 6px;
                selection-background-color: {Palette.ACCENT};
                selection-color: #ffffff;
            }}
        """)
        # A block cursor reads as a terminal rather than a text editor.
        self.setCursorWidth(8)

    def set_font_size(self, size: int) -> None:
        self._font_size = max(6, min(28, size))
        self._configure_appearance()
        self._emit_geometry()

    def zoom_in_step(self) -> None:
        self.set_font_size(self._font_size + 1)

    def zoom_out_step(self) -> None:
        self.set_font_size(self._font_size - 1)

    # ── connection state ──────────────────────────────────────────────────────

    def set_connected(self, connected: bool) -> None:
        """Enable/disable input, and announce a session that has actually ended.

        The banner is only printed on a connected → disconnected transition. The
        state machine also passes through CONNECTING (which is not connected),
        and printing "Session closed" while still dialling out is nonsense.
        """
        was_connected = self._connected
        self._connected = connected
        if connected:
            self._parser.reset()
        elif was_connected:
            self.write_notice("\n[ Session closed ]\n", Palette.TEXT_FAINT)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── incoming data ─────────────────────────────────────────────────────────

    def feed(self, data: str) -> None:
        """Accept raw device output. Rendering is deferred to the next frame."""
        if not data:
            return
        self._pending_events.extend(self._parser.feed(data))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush(self) -> None:
        """Apply every buffered event in one document edit."""
        if not self._pending_events:
            return
        events, self._pending_events = self._pending_events, []

        scrollbar = self.verticalScrollBar()
        # Only follow the tail if the user is already there; otherwise they are
        # reading scrollback and yanking them to the bottom would be hostile.
        follow = scrollbar.value() >= scrollbar.maximum() - 4

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try:
            for event in events:
                self._apply(cursor, event)
        finally:
            cursor.endEditBlock()

        self.setTextCursor(cursor)
        self._trim_scrollback()

        if follow:
            scrollbar.setValue(scrollbar.maximum())

    def _apply(self, cursor: QTextCursor, event: AnsiEvent) -> None:
        """Replay one parsed event against the document."""
        kind = event.type

        if kind is EventType.TEXT:
            self._insert_overwriting(cursor, event)

        elif kind is EventType.CARRIAGE_RETURN:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)

        elif kind is EventType.LINE_FEED:
            # Anything to the right of the cursor on this line stays; move past
            # it before breaking, or a CR+LF pair would truncate the line.
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertBlock()

        elif kind is EventType.BACKSPACE:
            if not cursor.atBlockStart():
                cursor.movePosition(QTextCursor.MoveOperation.Left)

        elif kind is EventType.TAB:
            # Advance to the next 8-column tab stop.
            column = cursor.positionInBlock()
            self._insert_overwriting(
                cursor, AnsiEvent(EventType.TEXT,
                                  text=" " * (8 - (column % 8)), style=event.style))

        elif kind is EventType.ERASE_LINE:
            self._erase_line(cursor, event.arg)

        elif kind is EventType.ERASE_SCREEN:
            if event.arg == 2:
                self.clear()
                cursor.movePosition(QTextCursor.MoveOperation.End)
            elif event.arg == 0:
                cursor.movePosition(QTextCursor.MoveOperation.End,
                                    QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()

        elif kind is EventType.CURSOR_LEFT:
            for _ in range(event.count):
                if cursor.atBlockStart():
                    break
                cursor.movePosition(QTextCursor.MoveOperation.Left)

        elif kind is EventType.CURSOR_RIGHT:
            for _ in range(event.count):
                if cursor.atBlockEnd():
                    break
                cursor.movePosition(QTextCursor.MoveOperation.Right)

        elif kind is EventType.CURSOR_HOME:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)

        elif kind is EventType.BELL:
            QApplication.beep()

    def _insert_overwriting(self, cursor: QTextCursor, event: AnsiEvent) -> None:
        """Write text at the cursor, replacing what it covers on this line.

        This is the behaviour that distinguishes a terminal from a text editor,
        and it is what makes ``\\r``-based redraws (progress dots, ``--More--``
        erasure, command-history recall) look right.
        """
        cursor.setCharFormat(self._format_for(event.style))

        remaining_on_line = cursor.block().length() - 1 - cursor.positionInBlock()
        overwrite = min(len(event.text), max(0, remaining_on_line))
        if overwrite:
            cursor.movePosition(QTextCursor.MoveOperation.Right,
                                QTextCursor.MoveMode.KeepAnchor, overwrite)
        cursor.insertText(event.text)

    @staticmethod
    def _erase_line(cursor: QTextCursor, mode: int) -> None:
        if mode == 0:          # cursor → end of line
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
        elif mode == 1:        # start of line → cursor
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
        else:                  # whole line
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

    def _format_for(self, style) -> QTextCharFormat:
        """Build a Qt character format from a parsed ANSI style."""
        fmt = QTextCharFormat()
        foreground, background = style.resolved(Palette.TEXT, "#0d1117")

        colour = QColor(foreground)
        if style.dim:
            colour = colour.darker(150)
        fmt.setForeground(colour)

        if background and background != "#0d1117":
            fmt.setBackground(QColor(background))
        if style.bold:
            fmt.setFontWeight(700)
        if style.italic:
            fmt.setFontItalic(True)
        if style.underline:
            fmt.setFontUnderline(True)
        return fmt

    def _trim_scrollback(self) -> None:
        """Drop the oldest lines once the document exceeds the scrollback limit."""
        document = self.document()
        excess = document.blockCount() - self._scrollback
        if excess <= 0:
            return
        cursor = QTextCursor(document.firstBlock())
        cursor.beginEditBlock()
        for _ in range(excess):
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()          # remove the now-empty block separator
        cursor.endEditBlock()

    # ── locally generated output ──────────────────────────────────────────────

    def write_notice(self, text: str, colour: str = Palette.TEXT_MUTED) -> None:
        """Print an application message (not device output) into the log."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        fmt.setFontItalic(True)
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    # ── keyboard ──────────────────────────────────────────────────────────────

    #: Keys with a fixed escape sequence. IOS accepts the ANSI forms for arrows
    #: and maps them onto its own Ctrl-P/N/B/F history bindings.
    _KEY_SEQUENCES = {
        Qt.Key.Key_Up: "\x1b[A",
        Qt.Key.Key_Down: "\x1b[B",
        Qt.Key.Key_Right: "\x1b[C",
        Qt.Key.Key_Left: "\x1b[D",
        Qt.Key.Key_Home: "\x01",           # Ctrl-A: beginning of line
        Qt.Key.Key_End: "\x05",            # Ctrl-E: end of line
        Qt.Key.Key_Delete: "\x1b[3~",
        Qt.Key.Key_Return: "\r",
        Qt.Key.Key_Enter: "\r",
        Qt.Key.Key_Tab: "\t",
        Qt.Key.Key_Escape: "\x1b",
        # IOS expects DEL (0x7f) for backspace over SSH but accepts BS (0x08)
        # on a console; 0x7f is correct for both in practice.
        Qt.Key.Key_Backspace: "\x7f",
    }

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        # ── local shortcuts, never forwarded to the device ────────────────────
        # Ctrl+Shift+C/V so plain Ctrl+C stays available as the IOS break key.
        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy()
            return
        if ctrl and shift and key == Qt.Key.Key_V:
            self._paste_clipboard()
            return
        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in_step()
            return
        if ctrl and key == Qt.Key.Key_Minus:
            self.zoom_out_step()
            return
        # Let the scrollback keys work as navigation.
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) and shift:
            super().keyPressEvent(event)
            return

        if not self._connected:
            return

        # ── Ctrl-<letter> → the corresponding control code ────────────────────
        if ctrl and not shift and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.data_entered.emit(chr(key - Qt.Key.Key_A + 1))
            return
        if ctrl and key == Qt.Key.Key_BracketRight:
            # Ctrl-] — the telnet escape, and IOS's "abort" for a hung session.
            self.data_entered.emit("\x1d")
            return

        sequence = self._KEY_SEQUENCES.get(key)
        if sequence is not None:
            self.data_entered.emit(sequence)
            return

        text = event.text()
        if text:
            self.data_entered.emit(text)
            if self._local_echo:
                self.feed(text)
            return

        # Anything unhandled (pure modifier presses, F-keys) is dropped rather
        # than passed to QTextEdit, which would try to edit a read-only document.

    def _paste_clipboard(self) -> None:
        """Send clipboard contents as keystrokes.

        Newlines become carriage returns so a pasted config block executes as
        typed lines. Pasting into a device is a common way to apply config, so
        this needs to work, but it is also easy to do by accident — hence the
        size guard.
        """
        if not self._connected:
            return
        text = QApplication.clipboard().text()
        if not text:
            return

        if len(text) > 20000:
            self.write_notice(
                f"\n[ Paste of {len(text)} characters blocked — use a config "
                f"file instead ]\n", Palette.WARNING)
            return

        self.data_entered.emit(text.replace("\r\n", "\r").replace("\n", "\r"))

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt override
        """Route middle-click / menu paste through the keystroke path."""
        if source.hasText():
            self._paste_clipboard()

    # ── mouse ─────────────────────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_font_size(
                self._font_size + (1 if event.angleDelta().y() > 0 else -1))
            event.accept()
            return
        super().wheelEvent(event)

    def _show_context_menu(self, position) -> None:
        menu = QMenu(self)

        copy_action = menu.addAction("Copy")
        copy_action.setShortcut("Ctrl+Shift+C")
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)

        paste_action = menu.addAction("Paste")
        paste_action.setShortcut("Ctrl+Shift+V")
        paste_action.setEnabled(self._connected and bool(QApplication.clipboard().text()))
        paste_action.triggered.connect(self._paste_clipboard)

        menu.addSeparator()
        menu.addAction("Select All", self.selectAll)
        menu.addAction("Copy All (plain text)", self._copy_all_plain)
        menu.addSeparator()
        menu.addAction("Clear Scrollback", self.clear_terminal)

        menu.exec(self.mapToGlobal(position))

    def _copy_all_plain(self) -> None:
        QApplication.clipboard().setText(strip_ansi(self.toPlainText()))

    def clear_terminal(self) -> None:
        self.clear()
        self._parser.reset()

    # ── geometry ──────────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        """Report the visible character grid so the remote pty can match it."""
        metrics = QFontMetricsF(self.font())
        char_width = metrics.horizontalAdvance("M")
        line_height = metrics.lineSpacing()
        if char_width <= 0 or line_height <= 0:
            return
        columns = max(20, int((self.viewport().width() - 12) / char_width))
        rows = max(5, int((self.viewport().height() - 12) / line_height))
        self.resized.emit(columns, rows)
