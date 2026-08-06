"""Application bootstrap: logging, Qt setup, and the main entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import APP_NAME, ORG_NAME, __version__


def configure_logging(verbose: bool = False) -> Path | None:
    """Send logs to stderr and to a rotating file in the config directory."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)-38s  %(message)s",
        datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    log_path: Path | None = None
    try:
        from logging.handlers import RotatingFileHandler

        from .core.credentials import config_dir

        log_path = config_dir() / "ciscoiosbox.log"
        file_handler = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except Exception as exc:  # noqa: BLE001 - file logging is a nicety, not a need
        logging.getLogger(__name__).warning("File logging unavailable: %s", exc)

    # These libraries are extremely chatty at DEBUG and drown out our own logs.
    for noisy in ("paramiko", "paramiko.transport", "netmiko"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if verbose else logging.WARNING)

    return log_path


def check_dependencies() -> list[str]:
    """Return a list of missing required packages."""
    missing = []
    for module, package in (("PySide6", "PySide6"),
                            ("netmiko", "netmiko"),
                            ("serial", "pyserial")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def install_exception_hook() -> None:
    """Log uncaught exceptions instead of letting Qt swallow them silently.

    Without this, an exception raised inside a Qt slot prints a traceback and
    the application carries on in an unknown state, which is confusing to debug.
    """
    log = logging.getLogger("ciscoiosbox.unhandled")
    original_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            original_hook(exc_type, exc_value, exc_traceback)
            return
        log.critical("Unhandled exception",
                     exc_info=(exc_type, exc_value, exc_traceback))

        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle("Unexpected Error")
                box.setText("An unexpected error occurred.")
                box.setInformativeText(f"{exc_type.__name__}: {exc_value}")
                box.setDetailedText("".join(
                    __import__("traceback").format_exception(
                        exc_type, exc_value, exc_traceback)))
                box.exec()
        except Exception:  # noqa: BLE001 - the error dialog must never itself crash
            pass

    sys.excepthook = hook


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ciscoiosbox",
        description="A lightweight desktop manager for Cisco switches and routers.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_path = configure_logging(args.verbose)
    log = logging.getLogger(__name__)
    log.info("Starting %s %s", APP_NAME, __version__)
    if log_path:
        log.info("Logging to %s", log_path)

    missing = check_dependencies()
    if missing:
        message = ("Missing required packages: " + ", ".join(missing)
                   + "\n\nInstall them with:\n    pip install -r requirements.txt")
        print(message, file=sys.stderr)
        # Try to show it graphically too, in case this was launched by double-click.
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication(sys.argv)
            QMessageBox.critical(None, "Missing Dependencies", message)
        except Exception:  # noqa: BLE001
            pass
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # High-DPI scaling is on by default in Qt6, but rounding must be set before
    # the QApplication exists or fractional-scale displays render blurry.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)

    from .ui.theme import apply_theme

    apply_theme(app)
    install_exception_hook()

    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
