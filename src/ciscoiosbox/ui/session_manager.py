"""Saved-sessions sidebar.

A grouped tree of device profiles with connect / edit / duplicate / delete.
This is the WinBox-style "pick a device and go" entry point.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator,
    QVBoxLayout, QWidget,
)

from ..core.exceptions import CiscoIOSBoxError, VaultLocked
from ..core.models import ConnectionType, DeviceProfile
from ..core.session_store import SessionStore
from .session_dialog import SessionDialog
from .theme import Palette

log = logging.getLogger(__name__)

#: Role used to stash the profile id on a tree item.
PROFILE_ROLE = Qt.ItemDataRole.UserRole + 1

#: Small glyphs distinguishing transports at a glance.
TYPE_ICONS = {
    ConnectionType.SSH: "🔒",
    ConnectionType.TELNET: "⌨",
    ConnectionType.SERIAL: "🔌",
}


class SessionManagerPanel(QWidget):
    """Sidebar listing every saved device profile."""

    #: The user wants to open a session for this profile.
    connect_requested = Signal(object)        # DeviceProfile
    #: Something changed on disk; the main window may want to react.
    store_changed = Signal()

    def __init__(self, store: SessionStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._build_ui()
        self.reload()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QLabel("Saved Sessions")
        header.setProperty("heading", True)
        layout.addWidget(header)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by name, host or group…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)

        self.empty_label = QLabel(
            "No saved sessions yet.\n\nClick “New” to add your first device.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("muted", True)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        self.empty_label.hide()

        buttons = QHBoxLayout()
        buttons.setSpacing(6)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setProperty("accent", True)
        self.connect_button.clicked.connect(self._connect_selected)
        buttons.addWidget(self.connect_button, 1)

        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.create_profile)
        buttons.addWidget(self.new_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit_selected)
        buttons.addWidget(self.edit_button)

        layout.addLayout(buttons)

        self.storage_label = QLabel()
        self.storage_label.setProperty("muted", True)
        self.storage_label.setWordWrap(True)
        self.storage_label.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px;")
        layout.addWidget(self.storage_label)
        self._update_storage_label()

        self._update_buttons()

    def _update_storage_label(self) -> None:
        credentials = self.store.credentials
        if credentials.is_persistent:
            self.storage_label.setText(f"Credentials: {credentials.backend_name}")
            self.storage_label.setStyleSheet(
                f"color: {Palette.TEXT_FAINT}; font-size: 11px;")
        else:
            self.storage_label.setText(
                "Credentials are not being saved — no keychain is available. "
                "Use File → Unlock Credential Vault to enable an encrypted vault.")
            self.storage_label.setStyleSheet(
                f"color: {Palette.WARNING}; font-size: 11px;")

    # ── population ────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Rebuild the tree from the store, preserving the selection."""
        selected = self.selected_profile_id()

        self.tree.clear()
        profiles = self.store.profiles

        # Group headers first, then ungrouped profiles at the root.
        group_items: dict[str, QTreeWidgetItem] = {}
        for group in sorted({p.group for p in profiles if p.group}):
            item = QTreeWidgetItem(self.tree, [group])
            font = QFont()
            font.setBold(True)
            item.setFont(0, font)
            item.setForeground(0, QColor(Palette.TEXT_MUTED))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)      # headers are not selectable
            item.setExpanded(True)
            group_items[group] = item

        for profile in sorted(profiles, key=lambda p: p.name.lower()):
            parent = group_items.get(profile.group, self.tree)
            item = QTreeWidgetItem(parent)
            self._decorate(item, profile)

        self.tree.expandAll()

        has_profiles = bool(profiles)
        self.tree.setVisible(has_profiles)
        self.empty_label.setVisible(not has_profiles)

        if selected:
            self.select_profile(selected)
        self._update_buttons()
        self._apply_filter(self.search_edit.text())
        self._update_storage_label()

    @staticmethod
    def _decorate(item: QTreeWidgetItem, profile: DeviceProfile) -> None:
        icon = TYPE_ICONS.get(profile.connection_type, "•")
        item.setText(0, f"{icon}  {profile.name}")
        item.setData(0, PROFILE_ROLE, profile.profile_id)
        tooltip = [
            f"<b>{profile.name}</b>",
            f"{profile.connection_type.label} → {profile.display_target}",
        ]
        if profile.username:
            tooltip.append(f"User: {profile.username}")
        if profile.snmp.enabled:
            tooltip.append(f"SNMP v{profile.snmp.version} enabled")
        if profile.notes:
            tooltip.append(f"<i>{profile.notes[:200]}</i>")
        item.setToolTip(0, "<br>".join(tooltip))

    def _apply_filter(self, text: str) -> None:
        """Hide non-matching profiles, and any group left with no children."""
        needle = text.strip().lower()

        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            profile_id = top.data(0, PROFILE_ROLE)

            if profile_id:                       # ungrouped profile at the root
                top.setHidden(not self._matches(profile_id, needle))
                continue

            visible_children = 0
            for child_index in range(top.childCount()):
                child = top.child(child_index)
                matches = self._matches(child.data(0, PROFILE_ROLE), needle)
                child.setHidden(not matches)
                visible_children += int(matches)
            top.setHidden(visible_children == 0)

    def _matches(self, profile_id: str, needle: str) -> bool:
        if not needle:
            return True
        profile = self.store.get(profile_id)
        if profile is None:
            return False
        haystack = " ".join([
            profile.name, profile.host, profile.group, profile.username,
            profile.serial.port, profile.notes,
        ]).lower()
        return needle in haystack

    # ── selection ─────────────────────────────────────────────────────────────

    def selected_profile_id(self) -> str:
        items = self.tree.selectedItems()
        if not items:
            return ""
        return items[0].data(0, PROFILE_ROLE) or ""

    def selected_profile(self) -> DeviceProfile | None:
        profile_id = self.selected_profile_id()
        return self.store.get(profile_id) if profile_id else None

    def select_profile(self, profile_id: str) -> None:
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, PROFILE_ROLE) == profile_id:
                self.tree.setCurrentItem(item)
                return
            iterator += 1

    def _update_buttons(self) -> None:
        has_selection = bool(self.selected_profile_id())
        self.connect_button.setEnabled(has_selection)
        self.edit_button.setEnabled(has_selection)

    # ── actions ───────────────────────────────────────────────────────────────

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, PROFILE_ROLE):
            self._connect_selected()

    def _connect_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        self._emit_connect(profile)

    def _emit_connect(self, profile: DeviceProfile) -> None:
        """Load secrets, prompt for anything missing, then request a connection."""
        try:
            profile = self.store.hydrate(profile)
        except VaultLocked:
            QMessageBox.information(
                self, "Vault Locked",
                "The credential vault is locked. Unlock it from the File menu to "
                "use saved passwords.")
        except CiscoIOSBoxError as exc:
            QMessageBox.warning(self, "Credentials", exc.user_message)

        # A profile with no stored password needs one now — unless it is a serial
        # console, which often has no authentication at all.
        needs_password = (
            not profile.password
            and profile.connection_type is not ConnectionType.SERIAL
            and profile.username
        )
        if needs_password:
            from PySide6.QtWidgets import QInputDialog

            password, ok = QInputDialog.getText(
                self, "Password Required",
                f"Password for {profile.username}@{profile.display_target}:",
                QLineEdit.EchoMode.Password)
            if not ok:
                return
            profile.password = password

        self.connect_requested.emit(profile)

    def create_profile(self) -> None:
        dialog = SessionDialog(None, self)
        dialog.set_storage_note(self._storage_note())
        dialog.connect_requested.connect(self._save_and_connect)
        if dialog.exec() == SessionDialog.DialogCode.Accepted:
            self._save(dialog.result_profile())

    def _edit_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        # Load the stored secrets so the dialog shows the real values rather than
        # blanking them — which would silently erase them on save.
        try:
            self.store.hydrate(profile)
        except CiscoIOSBoxError:
            log.debug("Could not hydrate profile for editing", exc_info=True)

        dialog = SessionDialog(profile, self)
        dialog.set_storage_note(self._storage_note())
        dialog.connect_requested.connect(self._save_and_connect)
        if dialog.exec() == SessionDialog.DialogCode.Accepted:
            self._save(dialog.result_profile())

    def _storage_note(self) -> str:
        credentials = self.store.credentials
        if credentials.is_persistent:
            return f"Saved to: {credentials.backend_name}."
        return ("No credential store is available, so passwords will be kept for "
                "this session only. Unlock the encrypted vault from the File menu "
                "to save them permanently.")

    def _save(self, profile: DeviceProfile) -> None:
        try:
            if self.store.get(profile.profile_id) is None:
                self.store.add(profile)
            else:
                self.store.update(profile)
        except VaultLocked:
            QMessageBox.warning(
                self, "Vault Locked",
                "The credential vault is locked, so the password could not be "
                "saved. The rest of the profile was saved.")
        except CiscoIOSBoxError as exc:
            QMessageBox.critical(self, "Could Not Save", str(exc))
            return
        self.reload()
        self.select_profile(profile.profile_id)
        self.store_changed.emit()

    def _save_and_connect(self, profile: DeviceProfile) -> None:
        self._save(profile)
        self._emit_connect(profile)

    def _duplicate_selected(self) -> None:
        profile_id = self.selected_profile_id()
        if not profile_id:
            return
        clone = self.store.duplicate(profile_id)
        self.reload()
        if clone is not None:
            self.select_profile(clone.profile_id)
        self.store_changed.emit()

    def _delete_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete Session")
        confirm.setText(f"Delete the saved session “{profile.name}”?")
        confirm.setInformativeText(
            "The profile and any stored credentials will be removed. "
            "This cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        try:
            self.store.remove(profile.profile_id)
        except CiscoIOSBoxError as exc:
            QMessageBox.critical(self, "Could Not Delete", str(exc))
            return
        self.reload()
        self.store_changed.emit()

    def _show_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None or not item.data(0, PROFILE_ROLE):
            menu = QMenu(self)
            menu.addAction("New Session…", self.create_profile)
            menu.exec(self.tree.viewport().mapToGlobal(position))
            return

        self.tree.setCurrentItem(item)
        menu = QMenu(self)

        connect = QAction("Connect", menu)
        connect.triggered.connect(self._connect_selected)
        font = connect.font()
        font.setBold(True)
        connect.setFont(font)
        menu.addAction(connect)

        menu.addSeparator()
        menu.addAction("Edit…", self._edit_selected)
        menu.addAction("Duplicate", self._duplicate_selected)
        menu.addSeparator()
        delete = menu.addAction("Delete…", self._delete_selected)
        delete.setIcon(self.style().standardIcon(
            self.style().StandardPixmap.SP_TrashIcon))

        menu.exec(self.tree.viewport().mapToGlobal(position))
