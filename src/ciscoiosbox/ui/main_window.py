"""Main application window: session sidebar plus a tab per connected device."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .. import APP_NAME, ORG_NAME, __version__
from ..core.exceptions import CiscoIOSBoxError, VaultLocked
from ..core.models import DeviceProfile
from ..core.session_store import SessionStore
from .device_tab import DeviceTab
from .session_manager import SessionManagerPanel
from .theme import Palette
from .widgets.toast import ToastManager

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Hosts the session sidebar and the connected-device tabs."""

    def __init__(self, store: SessionStore | None = None) -> None:
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)

        try:
            self.store = store or SessionStore()
        except CiscoIOSBoxError as exc:
            QMessageBox.warning(self, "Saved Sessions", str(exc))
            self.store = SessionStore(path=_fallback_store_path())

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1360, 860)
        self.setMinimumSize(940, 620)

        self._build_ui()
        self._build_menu()
        self._restore_geometry()

        self.toasts = ToastManager(self)
        # Toasts are positioned relative to the window, so they need a first
        # placement once the initial layout has settled.
        QTimer.singleShot(0, self.toasts._reposition)

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.session_panel = SessionManagerPanel(self.store)
        self.session_panel.setMinimumWidth(220)
        self.session_panel.setMaximumWidth(460)
        self.session_panel.connect_requested.connect(self.connect_to)
        self.splitter.addWidget(self.session_panel)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._update_actions)
        right_layout.addWidget(self.tabs)

        self.placeholder = QLabel(
            "<div style='text-align:center'>"
            f"<p style='font-size:19px;color:{Palette.TEXT_MUTED}'>{APP_NAME}</p>"
            f"<p style='color:{Palette.TEXT_FAINT}'>"
            "Select a saved session and press Connect,<br>"
            "or create a new one to get started.</p></div>")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setTextFormat(Qt.TextFormat.RichText)
        right_layout.addWidget(self.placeholder)

        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 1080])

        self.setCentralWidget(self.splitter)

        self.status = self.statusBar()
        self.status_message = QLabel("Ready")
        self.status.addWidget(self.status_message, 1)
        self.backend_label = QLabel()
        self.status.addPermanentWidget(self.backend_label)
        self._update_backend_label()

        self._update_placeholder()

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        # ── File ──
        file_menu = menubar.addMenu("&File")

        new_session = QAction("&New Session…", self)
        new_session.setShortcut(QKeySequence.StandardKey.New)
        new_session.triggered.connect(self.session_panel.create_profile)
        file_menu.addAction(new_session)

        file_menu.addSeparator()

        self.unlock_action = QAction("&Unlock Credential Vault…", self)
        self.unlock_action.setStatusTip(
            "Open an encrypted vault to store passwords when no OS keychain is available")
        self.unlock_action.triggered.connect(self._unlock_vault)
        file_menu.addAction(self.unlock_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # ── Session ──
        session_menu = menubar.addMenu("&Session")

        self.connect_action = QAction("&Connect", self)
        self.connect_action.setShortcut("Ctrl+Return")
        self.connect_action.triggered.connect(self.session_panel._connect_selected)
        session_menu.addAction(self.connect_action)

        self.disconnect_action = QAction("&Disconnect", self)
        self.disconnect_action.setShortcut("Ctrl+D")
        self.disconnect_action.triggered.connect(self._disconnect_current)
        session_menu.addAction(self.disconnect_action)

        self.reconnect_action = QAction("&Reconnect", self)
        self.reconnect_action.setShortcut("Ctrl+R")
        self.reconnect_action.triggered.connect(self._reconnect_current)
        session_menu.addAction(self.reconnect_action)

        session_menu.addSeparator()

        self.close_tab_action = QAction("Close &Tab", self)
        self.close_tab_action.setShortcut(QKeySequence.StandardKey.Close)
        self.close_tab_action.triggered.connect(
            lambda: self._close_tab(self.tabs.currentIndex()))
        session_menu.addAction(self.close_tab_action)

        # ── View ──
        view_menu = menubar.addMenu("&View")

        self.refresh_action = QAction("&Refresh All", self)
        self.refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.refresh_action.triggered.connect(self._refresh_current)
        view_menu.addAction(self.refresh_action)

        view_menu.addSeparator()

        toggle_sidebar = QAction("Toggle &Sidebar", self)
        toggle_sidebar.setShortcut("Ctrl+B")
        toggle_sidebar.triggered.connect(self._toggle_sidebar)
        view_menu.addAction(toggle_sidebar)

        # ── Help ──
        help_menu = menubar.addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

        self._update_actions()

    # ── connections ───────────────────────────────────────────────────────────

    def connect_to(self, profile: DeviceProfile) -> None:
        """Open a new tab and start a session for ``profile``."""
        existing = self._find_tab(profile.profile_id)
        if existing is not None and existing.is_connected:
            answer = QMessageBox.question(
                self, "Already Connected",
                f"There is already an open session for “{profile.name}”.\n\n"
                f"Open a second connection to the same device?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                self.tabs.setCurrentWidget(existing)
                return

        tab = DeviceTab(profile, self)
        tab.notify.connect(self._on_notify)
        tab.title_changed.connect(self._on_title_changed)

        index = self.tabs.addTab(tab, profile.name)
        self.tabs.setTabToolTip(
            index, f"{profile.connection_type.label} → {profile.display_target}")
        self.tabs.setCurrentIndex(index)
        self._update_placeholder()

        self.status_message.setText(f"Connecting to {profile.display_target}…")
        tab.start()

    def _find_tab(self, profile_id: str) -> DeviceTab | None:
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, DeviceTab) and tab.profile.profile_id == profile_id:
                return tab
        return None

    def current_tab(self) -> DeviceTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, DeviceTab) else None

    def _close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if not isinstance(tab, DeviceTab):
            return

        if tab.is_connected:
            answer = QMessageBox.question(
                self, "Close Session",
                f"“{tab.title}” is still connected.\n\nDisconnect and close the tab?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.tabs.removeTab(index)
        tab.close_tab()
        tab.deleteLater()
        self._update_placeholder()

    def _disconnect_current(self) -> None:
        tab = self.current_tab()
        if tab is not None and tab.is_connected:
            tab.disconnect_device()
            self.status_message.setText(f"Disconnected from {tab.title}.")

    def _reconnect_current(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        profile = tab.profile
        index = self.tabs.indexOf(tab)
        self.tabs.removeTab(index)
        tab.close_tab()
        tab.deleteLater()
        self.connect_to(profile)

    def _refresh_current(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.refresh_all()
            self.status_message.setText("Refreshing…")

    # ── UI state ──────────────────────────────────────────────────────────────

    def _update_placeholder(self) -> None:
        has_tabs = self.tabs.count() > 0
        self.tabs.setVisible(has_tabs)
        self.placeholder.setVisible(not has_tabs)
        self._update_actions()

    def _update_actions(self) -> None:
        # _build_ui() runs before _build_menu(), and both paths land here.
        if not hasattr(self, "disconnect_action"):
            return
        tab = self.current_tab()
        connected = tab is not None and tab.is_connected
        for action in (self.disconnect_action, self.refresh_action):
            action.setEnabled(connected)
        for action in (self.reconnect_action, self.close_tab_action):
            action.setEnabled(tab is not None)

    def _on_title_changed(self, tab: DeviceTab, title: str) -> None:
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, title)
        if tab is self.current_tab():
            self.setWindowTitle(f"{title} — {APP_NAME}")
        self._update_actions()

    def _on_notify(self, tab: DeviceTab, message: str, level: str) -> None:
        # Prefix the device name when the message comes from a background tab,
        # so a toast is never ambiguous about which device it refers to.
        if tab is not self.current_tab():
            message = f"{tab.title}: {message}"

        {
            "success": self.toasts.success,
            "warning": self.toasts.warning,
            "error": self.toasts.error,
        }.get(level, self.toasts.info)(message)

        self.status_message.setText(message)

    def _toggle_sidebar(self) -> None:
        self.session_panel.setVisible(not self.session_panel.isVisible())

    def _update_backend_label(self) -> None:
        credentials = self.store.credentials
        if credentials.is_persistent:
            self.backend_label.setText(f"🔐 {credentials.backend_name}")
            self.backend_label.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        else:
            self.backend_label.setText("🔓 Credentials not saved")
            self.backend_label.setStyleSheet(f"color: {Palette.WARNING};")

    # ── credential vault ──────────────────────────────────────────────────────

    def _unlock_vault(self) -> None:
        dialog = VaultDialog(self.store.credentials.vault_exists(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.store.credentials.use_vault(dialog.password())
        except VaultLocked as exc:
            QMessageBox.warning(self, "Incorrect Password", exc.user_message)
            return
        except CiscoIOSBoxError as exc:
            QMessageBox.critical(self, "Vault Error", str(exc))
            return

        self._update_backend_label()
        self.session_panel._update_storage_label()
        self.toasts.success("Credential vault unlocked.")

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>A lightweight desktop manager for Cisco switches and routers.</p>"
            "<p style='color:#9aa4b2'>Connects over SSH, Telnet and serial "
            "console. All network I/O runs on background threads.</p>")

    # ── geometry persistence ──────────────────────────────────────────────────

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/splitter")
        if state:
            self.splitter.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        open_sessions = [
            self.tabs.widget(i) for i in range(self.tabs.count())
            if isinstance(self.tabs.widget(i), DeviceTab)
            and self.tabs.widget(i).is_connected
        ]

        if open_sessions:
            answer = QMessageBox.question(
                self, "Quit",
                f"{len(open_sessions)} session(s) are still connected.\n\n"
                f"Disconnect and quit?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.saveState())

        # Shut every worker thread down before the event loop stops, or Qt will
        # complain about threads still running at exit. close_tab() returns
        # immediately (teardown runs in the background), so drain the event
        # loop until the threads have finished. The per-thread reaper
        # terminates any transport that wedges, bounding this wait.
        controllers = []
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, DeviceTab):
                controllers.append(tab.controller)
                tab.close_tab()

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if not any(c.thread_running for c in controllers):
                break
            QApplication.processEvents()
            time.sleep(0.02)

        event.accept()


class VaultDialog(QDialog):
    """Prompts for the master password protecting the encrypted vault."""

    def __init__(self, vault_exists: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Credential Vault")
        self.setModal(True)
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        if vault_exists:
            explanation = ("Enter the master password to unlock your saved "
                           "credentials.")
        else:
            explanation = (
                "No OS keychain is available, so credentials can be stored in an "
                "encrypted file instead.\n\n"
                "Choose a master password. It is never written to disk — if you "
                "lose it, the saved credentials cannot be recovered.")

        label = QLabel(explanation)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Master password")
        self.password_edit.textChanged.connect(self._validate)
        layout.addWidget(self.password_edit)

        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.setPlaceholderText("Confirm master password")
        self.confirm_edit.textChanged.connect(self._validate)
        self.confirm_edit.setVisible(not vault_exists)
        layout.addWidget(self.confirm_edit)

        self.error_label = QLabel()
        self.error_label.setProperty("error", True)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Unlock" if vault_exists else "Create Vault")
        self.ok_button.setProperty("accent", True)
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._vault_exists = vault_exists

    def _validate(self) -> None:
        password = self.password_edit.text()
        problem = ""

        if not self._vault_exists:
            if len(password) < 8:
                problem = "Use at least 8 characters."
            elif self.confirm_edit.text() and password != self.confirm_edit.text():
                problem = "The passwords do not match."
            elif not self.confirm_edit.text():
                problem = ""

        self.error_label.setText(problem)
        valid = bool(password) and not problem
        if not self._vault_exists:
            valid = valid and password == self.confirm_edit.text()
        self.ok_button.setEnabled(valid)

    def password(self) -> str:
        return self.password_edit.text()


def _fallback_store_path():
    """Somewhere writable to keep going when the real store cannot be read."""
    import tempfile
    from pathlib import Path

    return Path(tempfile.gettempdir()) / "ciscoiosbox-sessions.json"
