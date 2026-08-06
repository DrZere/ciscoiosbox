"""Persistence for saved device profiles.

Profiles live in a plain JSON file (readable, diffable, easy to back up).
Secrets never appear in it — they are handed to :class:`CredentialStore`, which
puts them in the OS keychain or an encrypted vault.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .credentials import CredentialStore, config_dir
from .exceptions import CiscoIOSBoxError, VaultLocked
from .models import DeviceProfile

log = logging.getLogger(__name__)


class SessionStore:
    """Loads, saves and mutates the saved-session list."""

    def __init__(self, path: Path | None = None,
                 credentials: CredentialStore | None = None) -> None:
        self.path = path or (config_dir() / "sessions.json")
        self.credentials = credentials or CredentialStore()
        self._profiles: list[DeviceProfile] = []
        self.load()

    # ── disk I/O ──────────────────────────────────────────────────────────────

    def load(self) -> list[DeviceProfile]:
        """Read profiles from disk. A missing file yields an empty list."""
        self._profiles = []
        if not self.path.exists():
            return self._profiles

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Preserve the damaged file rather than silently overwriting it.
            log.error("Could not read %s: %s", self.path, exc)
            raise CiscoIOSBoxError(
                f"The saved sessions file could not be read. It has been left "
                f"untouched at {self.path}.", detail=str(exc)) from exc

        for entry in raw.get("profiles", []):
            try:
                self._profiles.append(DeviceProfile.from_dict(entry))
            except Exception:  # noqa: BLE001 - skip one bad entry, keep the rest
                log.warning("Skipping malformed profile entry: %r", entry, exc_info=True)
        return self._profiles

    def save(self) -> None:
        """Write profiles atomically so a crash can't corrupt the list."""
        payload = {
            "version": 1,
            "profiles": [p.to_dict() for p in self._profiles],
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise CiscoIOSBoxError(
                "Could not save the session list.", detail=str(exc)) from exc

    # ── queries ───────────────────────────────────────────────────────────────

    @property
    def profiles(self) -> list[DeviceProfile]:
        return list(self._profiles)

    def get(self, profile_id: str) -> DeviceProfile | None:
        return next((p for p in self._profiles if p.profile_id == profile_id), None)

    def groups(self) -> list[str]:
        """Distinct group labels, for the session-list tree."""
        return sorted({p.group for p in self._profiles if p.group})

    # ── mutations ─────────────────────────────────────────────────────────────

    def add(self, profile: DeviceProfile) -> None:
        self._profiles.append(profile)
        self._persist_secrets(profile)
        self.save()

    def update(self, profile: DeviceProfile) -> None:
        """Replace an existing profile, matched on ``profile_id``."""
        for index, existing in enumerate(self._profiles):
            if existing.profile_id == profile.profile_id:
                self._profiles[index] = profile
                break
        else:
            self._profiles.append(profile)
        self._persist_secrets(profile)
        self.save()

    def remove(self, profile_id: str) -> None:
        profile = self.get(profile_id)
        if profile is None:
            return
        self._profiles = [p for p in self._profiles if p.profile_id != profile_id]
        try:
            self.credentials.forget(profile_id)
        except Exception:  # noqa: BLE001 - a stale keychain entry is not fatal
            log.debug("Could not purge secrets for %s", profile_id, exc_info=True)
        self.save()

    def duplicate(self, profile_id: str) -> DeviceProfile | None:
        """Clone a profile (new id, "(copy)" suffix) including its secrets."""
        import copy
        import uuid

        source = self.get(profile_id)
        if source is None:
            return None
        clone = copy.deepcopy(source)
        clone.profile_id = uuid.uuid4().hex
        clone.name = f"{source.name} (copy)"
        try:
            secrets_map = self.credentials.load_secrets(profile_id)
            clone.password = secrets_map.get("password", "")
            clone.enable_password = secrets_map.get("enable_password", "")
        except VaultLocked:
            pass
        self.add(clone)
        return clone

    # ── secrets bridging ──────────────────────────────────────────────────────

    def _persist_secrets(self, profile: DeviceProfile) -> None:
        """Push a profile's in-memory secrets into the credential store."""
        if not profile.save_password:
            # The user asked us not to remember — clear anything already stored.
            try:
                self.credentials.forget(profile.profile_id)
            except Exception:  # noqa: BLE001
                log.debug("forget() failed", exc_info=True)
            return

        if not self.credentials.is_persistent:
            log.info("No persistent credential backend; secrets kept in memory only.")
            return

        try:
            self.credentials.save_secrets(profile.profile_id, {
                "password": profile.password,
                "enable_password": profile.enable_password,
                "snmp_community": profile.snmp.community,
                "snmp_auth_key": profile.snmp.auth_key,
                "snmp_priv_key": profile.snmp.priv_key,
            })
        except VaultLocked:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Could not persist secrets for %s: %s", profile.name, exc)

    def hydrate(self, profile: DeviceProfile) -> DeviceProfile:
        """Return the profile with its secrets loaded from the credential store.

        Called just before connecting. Mutates and returns the same instance so
        callers can chain.
        """
        try:
            secrets_map = self.credentials.load_secrets(profile.profile_id)
        except VaultLocked:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load secrets for %s: %s", profile.name, exc)
            return profile

        # Do not clobber a password the user typed into the connect dialog.
        profile.password = profile.password or secrets_map.get("password", "")
        profile.enable_password = (
            profile.enable_password or secrets_map.get("enable_password", ""))
        profile.snmp.community = (
            profile.snmp.community or secrets_map.get("snmp_community", ""))
        profile.snmp.auth_key = profile.snmp.auth_key or secrets_map.get("snmp_auth_key", "")
        profile.snmp.priv_key = profile.snmp.priv_key or secrets_map.get("snmp_priv_key", "")
        return profile
