"""ANSI parser tests. No Qt needed — the parser is deliberately pure."""
from __future__ import annotations

import pytest

from ciscoiosbox.ui.ansi import AnsiParser, EventType, strip_ansi


def types_of(events):
    return [e.type for e in events]


def text_of(events):
    return "".join(e.text for e in events if e.type is EventType.TEXT)


def test_plain_text():
    events = AnsiParser().feed("Switch#")
    assert types_of(events) == [EventType.TEXT]
    assert events[0].text == "Switch#"


def test_sgr_colour_applies_to_following_text():
    events = AnsiParser().feed("\x1b[32mgreen\x1b[0mplain")
    coloured, plain = [e for e in events if e.type is EventType.TEXT]
    assert coloured.text == "green"
    assert coloured.style.foreground == "#98c379"
    assert plain.style.foreground == ""       # reset


def test_control_characters():
    events = AnsiParser().feed("a\rb\nc\bd\te")
    assert types_of(events) == [
        EventType.TEXT, EventType.CARRIAGE_RETURN,
        EventType.TEXT, EventType.LINE_FEED,
        EventType.TEXT, EventType.BACKSPACE,
        EventType.TEXT, EventType.TAB,
        EventType.TEXT,
    ]


def test_erase_line_and_screen():
    events = AnsiParser().feed("\x1b[K\x1b[2K\x1b[2J")
    assert types_of(events) == [
        EventType.ERASE_LINE, EventType.ERASE_LINE, EventType.ERASE_SCREEN]
    assert events[0].arg == 0
    assert events[1].arg == 2
    assert events[2].arg == 2


def test_escape_split_across_reads():
    """A sequence arriving in two chunks must not print as garbage.

    This happens constantly on a slow serial line, and is the single most
    common way a naive terminal emulator corrupts its output.
    """
    parser = AnsiParser()
    first = parser.feed("hello \x1b[3")
    second = parser.feed("1mRED")

    assert text_of(first) == "hello "
    assert "\x1b" not in text_of(first)
    red = [e for e in second if e.type is EventType.TEXT][0]
    assert red.text == "RED"
    assert red.style.foreground == "#e06c75"


def test_style_persists_across_feeds():
    parser = AnsiParser()
    parser.feed("\x1b[33m")
    events = parser.feed("still yellow")
    assert events[0].style.foreground == "#e5c07b"


def test_xterm256_and_truecolor():
    parser = AnsiParser()
    assert parser.feed("\x1b[38;5;196mX")[0].style.foreground == "#ff0000"
    parser.feed("\x1b[0m")
    assert parser.feed("\x1b[38;2;100;200;50mY")[0].style.foreground == "#64c832"


def test_attributes():
    parser = AnsiParser()
    event = parser.feed("\x1b[1;4;7mstyled")[0]
    assert event.style.bold and event.style.underline and event.style.reverse
    event = parser.feed("\x1b[22;24;27mplain")[0]
    assert not (event.style.bold or event.style.underline or event.style.reverse)


def test_osc_sequences_are_dropped():
    """Window-title sequences must vanish rather than print."""
    events = AnsiParser().feed("\x1b]0;my title\x07visible")
    assert text_of(events) == "visible"


def test_nulls_are_dropped():
    """Serial links pad with NULs; they must not appear on screen."""
    assert text_of(AnsiParser().feed("a\x00b")) == "ab"


def test_more_prompt_erasure_sequence():
    """The exact sequence IOS uses to wipe its '--More--' prompt."""
    events = AnsiParser().feed("--More--\r\x1b[K")
    assert types_of(events) == [
        EventType.TEXT, EventType.CARRIAGE_RETURN, EventType.ERASE_LINE]


def test_reverse_video_swaps_colours():
    from ciscoiosbox.ui.ansi import TextStyle

    style = TextStyle(foreground="#ff0000", background="#000000", reverse=True)
    assert style.resolved("#ffffff", "#111111") == ("#000000", "#ff0000")


@pytest.mark.parametrize("raw,expected", [
    ("\x1b[32mSwitch#\x1b[0m ok", "Switch# ok"),
    ("\x1b]0;title\x07text", "text"),
    ("plain", "plain"),
])
def test_strip_ansi(raw, expected):
    assert strip_ansi(raw) == expected
