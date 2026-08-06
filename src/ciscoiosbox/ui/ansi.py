"""ANSI escape-sequence parser.

Pure logic with no Qt dependency, so it can be unit-tested against captured
device output. It converts a byte stream into a list of :class:`AnsiEvent`
objects that the terminal widget replays against its document.

Scope: this targets what Cisco IOS actually emits — SGR colour, carriage
returns, backspace-based line editing, and erase-line/erase-screen. It is not a
full VT100: there is no alternate screen buffer or scroll region, because IOS
never uses them.

The parser is *streaming*: a sequence split across two reads (which happens
constantly on a slow serial line) is held in an internal buffer and resolved
when the rest arrives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum, auto


class EventType(Enum):
    TEXT = auto()
    CARRIAGE_RETURN = auto()
    LINE_FEED = auto()
    BACKSPACE = auto()
    TAB = auto()
    BELL = auto()
    ERASE_LINE = auto()        # arg 0=to end, 1=to start, 2=whole line
    ERASE_SCREEN = auto()      # arg 0=to end, 1=to start, 2=all
    CURSOR_LEFT = auto()
    CURSOR_RIGHT = auto()
    CURSOR_HOME = auto()


# Standard 8 colours, then their bright variants. Values are tuned for legibility
# on the app's dark background rather than being literal xterm defaults.
ANSI_COLOURS: dict[int, str] = {
    30: "#3b4048", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
    34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#abb2bf",
    90: "#5c6370", 91: "#ff7b72", 92: "#7ee787", 93: "#ffd479",
    94: "#79c0ff", 95: "#d2a8ff", 96: "#76e3ea", 97: "#ffffff",
}


@dataclass(frozen=True)
class TextStyle:
    """Character formatting carried by a TEXT event."""

    foreground: str = ""       # "" means "use the widget default"
    background: str = ""
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False

    def resolved(self, default_fg: str, default_bg: str) -> tuple[str, str]:
        """Return (fg, bg), applying the reverse-video swap if set."""
        foreground = self.foreground or default_fg
        background = self.background or default_bg
        if self.reverse:
            return background, foreground
        return foreground, background


@dataclass
class AnsiEvent:
    type: EventType
    text: str = ""
    style: TextStyle = field(default_factory=TextStyle)
    arg: int = 0
    count: int = 1


#: CSI sequence: ESC [ <params> <intermediate> <final byte>
_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
#: OSC sequence (window title etc.), terminated by BEL or ST.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
#: Two-character escapes we recognise and discard, e.g. ESC ( B.
_SIMPLE_ESC = re.compile(r"\x1b[()][A-Za-z0-9]")
#: Anything else starting with ESC that we skip once complete.
_OTHER_ESC = re.compile(r"\x1b[=>78Mc]")


def _xterm256_to_hex(index: int) -> str:
    """Convert an xterm-256 palette index to a hex colour."""
    if index < 16:
        base = ANSI_COLOURS.get(30 + index if index < 8 else 90 + (index - 8))
        return base or "#abb2bf"
    if index < 232:
        # 6x6x6 colour cube.
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[index // 36], levels[(index // 6) % 6], levels[index % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    # 24-step greyscale ramp.
    value = 8 + (index - 232) * 10
    return f"#{value:02x}{value:02x}{value:02x}"


class AnsiParser:
    """Streaming ANSI parser. One instance per terminal session."""

    def __init__(self) -> None:
        self.style = TextStyle()
        self._pending = ""

    def reset(self) -> None:
        self.style = TextStyle()
        self._pending = ""

    def feed(self, data: str) -> list[AnsiEvent]:
        """Consume a chunk and return the events it produced."""
        if not data:
            return []

        buffer = self._pending + data
        self._pending = ""
        events: list[AnsiEvent] = []
        position = 0
        length = len(buffer)
        plain_start = 0

        def flush_text(end: int) -> None:
            """Emit accumulated printable characters as one TEXT event."""
            if end > plain_start:
                events.append(AnsiEvent(
                    EventType.TEXT, text=buffer[plain_start:end], style=self.style))

        while position < length:
            char = buffer[position]

            if char == "\x1b":
                flush_text(position)
                consumed = self._handle_escape(buffer, position, events)
                if consumed == 0:
                    # Incomplete sequence at the end of the chunk: stash it and
                    # wait for the rest rather than printing escape garbage.
                    self._pending = buffer[position:]
                    return events
                position += consumed
                plain_start = position
                continue

            if char in "\r\n\b\t\x07":
                flush_text(position)
                events.append(self._control_event(char))
                position += 1
                plain_start = position
                continue

            if char == "\x00":
                # NULs are padding on some serial links; drop them silently.
                flush_text(position)
                position += 1
                plain_start = position
                continue

            position += 1

        flush_text(length)
        return events

    @staticmethod
    def _control_event(char: str) -> AnsiEvent:
        return {
            "\r": AnsiEvent(EventType.CARRIAGE_RETURN),
            "\n": AnsiEvent(EventType.LINE_FEED),
            "\b": AnsiEvent(EventType.BACKSPACE),
            "\t": AnsiEvent(EventType.TAB),
            "\x07": AnsiEvent(EventType.BELL),
        }[char]

    def _handle_escape(self, buffer: str, position: int,
                       events: list[AnsiEvent]) -> int:
        """Process one escape sequence. Returns bytes consumed, 0 if incomplete."""
        match = _CSI.match(buffer, position)
        if match:
            self._handle_csi(match.group(1), match.group(3), events)
            return match.end() - position

        match = _OSC.match(buffer, position)
        if match:
            return match.end() - position          # window titles: ignore

        for pattern in (_SIMPLE_ESC, _OTHER_ESC):
            match = pattern.match(buffer, position)
            if match:
                return match.end() - position

        remaining = buffer[position:]
        # A lone ESC (or a prefix of a longer sequence) at the buffer's end is
        # incomplete; ask the caller to wait for more data.
        if len(remaining) < 4 and not _CSI.search(remaining):
            return 0
        # Otherwise it is an escape we do not implement — skip just the ESC.
        return 1

    def _handle_csi(self, params: str, final: str, events: list[AnsiEvent]) -> None:
        """Dispatch a CSI sequence by its final byte."""
        numbers = self._parse_params(params)

        if final == "m":
            self._apply_sgr(numbers)
        elif final == "K":
            events.append(AnsiEvent(EventType.ERASE_LINE, arg=numbers[0] if numbers else 0))
        elif final == "J":
            events.append(AnsiEvent(EventType.ERASE_SCREEN, arg=numbers[0] if numbers else 0))
        elif final == "D":
            events.append(AnsiEvent(EventType.CURSOR_LEFT, count=max(1, numbers[0] if numbers else 1)))
        elif final == "C":
            events.append(AnsiEvent(EventType.CURSOR_RIGHT, count=max(1, numbers[0] if numbers else 1)))
        elif final in ("H", "f"):
            events.append(AnsiEvent(EventType.CURSOR_HOME))
        elif final == "G":
            # Cursor to absolute column; column 1 is the only case IOS uses.
            events.append(AnsiEvent(EventType.CARRIAGE_RETURN))
        # Everything else (scroll regions, device queries, cursor save/restore)
        # is intentionally dropped — IOS does not rely on it.

    @staticmethod
    def _parse_params(params: str) -> list[int]:
        if not params:
            return []
        values: list[int] = []
        for part in params.split(";"):
            part = part.strip()
            values.append(int(part) if part.isdigit() else 0)
        return values

    def _apply_sgr(self, numbers: list[int]) -> None:
        """Update the current style from an SGR (colour/attribute) sequence."""
        if not numbers:
            self.style = TextStyle()
            return

        index = 0
        while index < len(numbers):
            code = numbers[index]

            if code == 0:
                self.style = TextStyle()
            elif code == 1:
                self.style = replace(self.style, bold=True)
            elif code == 2:
                self.style = replace(self.style, dim=True)
            elif code == 3:
                self.style = replace(self.style, italic=True)
            elif code == 4:
                self.style = replace(self.style, underline=True)
            elif code == 7:
                self.style = replace(self.style, reverse=True)
            elif code in (21, 22):
                self.style = replace(self.style, bold=False, dim=False)
            elif code == 23:
                self.style = replace(self.style, italic=False)
            elif code == 24:
                self.style = replace(self.style, underline=False)
            elif code == 27:
                self.style = replace(self.style, reverse=False)
            elif code in ANSI_COLOURS and (30 <= code <= 37 or 90 <= code <= 97):
                self.style = replace(self.style, foreground=ANSI_COLOURS[code])
            elif 40 <= code <= 47:
                self.style = replace(self.style, background=ANSI_COLOURS[code - 10])
            elif 100 <= code <= 107:
                self.style = replace(self.style, background=ANSI_COLOURS[code - 10])
            elif code == 39:
                self.style = replace(self.style, foreground="")
            elif code == 49:
                self.style = replace(self.style, background="")
            elif code in (38, 48):
                # Extended colour: 5;<idx> (256-colour) or 2;<r>;<g>;<b>.
                consumed, colour = self._parse_extended_colour(numbers, index)
                if colour:
                    if code == 38:
                        self.style = replace(self.style, foreground=colour)
                    else:
                        self.style = replace(self.style, background=colour)
                index += consumed
            index += 1

    @staticmethod
    def _parse_extended_colour(numbers: list[int], index: int) -> tuple[int, str]:
        """Return (extra params consumed, hex colour)."""
        if index + 1 >= len(numbers):
            return 0, ""
        mode = numbers[index + 1]
        if mode == 5 and index + 2 < len(numbers):
            return 2, _xterm256_to_hex(numbers[index + 2])
        if mode == 2 and index + 4 < len(numbers):
            r, g, b = numbers[index + 2], numbers[index + 3], numbers[index + 4]
            return 4, f"#{r & 0xFF:02x}{g & 0xFF:02x}{b & 0xFF:02x}"
        return 1, ""


def strip_ansi(text: str) -> str:
    """Remove every escape sequence, for logging or copy-to-clipboard."""
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _SIMPLE_ESC.sub("", text)
    return _OTHER_ESC.sub("", text)
