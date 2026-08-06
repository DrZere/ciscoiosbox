"""Secure credential storage.

Two backends, chosen at runtime:

1. :class:`KeyringBackend` — the OS keychain (macOS Keychain, Windows Credential
   Manager via DPAPI, Linux SecretService/KWallet). Preferred: the operating
   system owns the encryption key and ties it to the user's login session, so
   nothing sensitive is ever written by this application.

2. :class:`EncryptedFileBackend` — a Fernet-encrypted vault file, used only when
   no keyring is available (common on headless Linux and inside some frozen
   builds). The key is derived from a user-supplied master password with
   scrypt; it is never stored on disk. Lose the master password and the vault
   is unrecoverable — which is the point.

Deliberate non-goal: there is no "obfuscated but keyless" mode. A vault whose
key sits next to it on disk offers no protection while implying that it does,
so if the user declines both backends we simply do not persist secrets.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .exceptions import CredentialError, VaultLocked

log = logging.getLogger(__name__)

#: Namespace used for keyring entries so they group in the OS credential UI.
KEYRING_SERVICE = "CiscoIOSBox"

#: Field names we manage per profile.
SECRET_FIELDS = ("password", "enable_password", "snmp_community",
                 "snmp_auth_key", "snmp_priv_key")


def config_dir() -> Path:
    """Per-user config directory, respecting platform conventions."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "CiscoIOSBox"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─── Backends ─────────────────────────────────────────────────────────────────

class SecretBackend(ABC):
    """Stores and retrieves one secret per (profile_id, field) pair."""

    #: Shown in the UI so the user knows where their secrets live.
    display_name = "unknown"

    @abstractmethod
    def get(self, profile_id: str, field: str) -> str: ...

    @abstractmethod
    def set(self, profile_id: str, field: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, profile_id: str, field: str) -> None: ...

    def delete_all(self, profile_id: str) -> None:
        for field in SECRET_FIELDS:
            try:
                self.delete(profile_id, field)
            except Exception:  # noqa: BLE001 - deleting a missing key is fine
                log.debug("Could not delete %s/%s", profile_id, field, exc_info=True)

    @property
    def is_unlocked(self) -> bool:
        return True


class NullBackend(SecretBackend):
    """Drops everything. Used when the user opts out of saving passwords."""

    display_name = "not saved (session only)"

    def get(self, profile_id: str, field: str) -> str:
        return ""

    def set(self, profile_id: str, field: str, value: str) -> None:
        pass

    def delete(self, profile_id: str, field: str) -> None:
        pass


class KeyringBackend(SecretBackend):
    """Delegates to the operating system's credential store."""

    display_name = "OS keychain"

    def __init__(self) -> None:
        import keyring

        self._keyring = keyring
        # Probe for a usable backend: keyring installs a "fail" backend when it
        # can't find a real one, and that only raises on first use.
        backend = keyring.get_keyring()
        name = type(backend).__name__.lower()
        if "fail" in name or "null" in name:
            raise CredentialError("No usable OS keychain backend is available.")
        self.display_name = f"OS keychain ({type(backend).__name__})"

    @staticmethod
    def _key(profile_id: str, field: str) -> str:
        return f"{profile_id}:{field}"

    def get(self, profile_id: str, field: str) -> str:
        try:
            return self._keyring.get_password(
                KEYRING_SERVICE, self._key(profile_id, field)) or ""
        except Exception as exc:  # noqa: BLE001
            raise CredentialError(
                "Could not read from the OS keychain.", detail=str(exc)) from exc

    def set(self, profile_id: str, field: str, value: str) -> None:
        key = self._key(profile_id, field)
        try:
            if value:
                self._keyring.set_password(KEYRING_SERVICE, key, value)
            else:
                self.delete(profile_id, field)
        except Exception as exc:  # noqa: BLE001
            raise CredentialError(
                "Could not write to the OS keychain.", detail=str(exc)) from exc

    def delete(self, profile_id: str, field: str) -> None:
        try:
            self._keyring.delete_password(KEYRING_SERVICE, self._key(profile_id, field))
        except Exception:  # noqa: BLE001 - absent entry is not an error
            log.debug("keyring delete no-op for %s/%s", profile_id, field)


class EncryptedFileBackend(SecretBackend):
    """Fernet vault keyed by a scrypt-derived master password."""

    display_name = "encrypted vault file"

    # scrypt parameters: ~100ms on modern hardware, cheap enough for an
    # interactive unlock and expensive enough to blunt offline guessing.
    _SCRYPT_N = 2 ** 15
    _SCRYPT_R = 8
    _SCRYPT_P = 1

    def __init__(self, vault_path: Path | None = None) -> None:
        self.path = vault_path or (config_dir() / "vault.enc")
        self._fernet = None
        self._data: dict[str, str] = {}
        self._salt: bytes = b""

    # ── locking ───────────────────────────────────────────────────────────────

    @property
    def is_unlocked(self) -> bool:
        return self._fernet is not None

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _derive(self, master_password: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        kdf = Scrypt(salt=salt, length=32, n=self._SCRYPT_N,
                     r=self._SCRYPT_R, p=self._SCRYPT_P)
        return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))

    def create(self, master_password: str) -> None:
        """Initialise a brand-new empty vault."""
        from cryptography.fernet import Fernet

        self._salt = secrets.token_bytes(16)
        self._fernet = Fernet(self._derive(master_password, self._salt))
        self._data = {}
        self._flush()

    def unlock(self, master_password: str) -> None:
        """Open an existing vault. Raises :class:`VaultLocked` on a bad password."""
        from cryptography.fernet import Fernet, InvalidToken

        if not self.exists:
            self.create(master_password)
            return

        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            self._salt = base64.b64decode(envelope["salt"])
            blob = base64.b64decode(envelope["data"])
        except Exception as exc:  # noqa: BLE001
            raise CredentialError(
                "The credential vault file is corrupt or unreadable.",
                detail=str(exc)) from exc

        fernet = Fernet(self._derive(master_password, self._salt))
        try:
            self._data = json.loads(fernet.decrypt(blob).decode("utf-8")) if blob else {}
        except InvalidToken as exc:
            raise VaultLocked("Incorrect master password.") from exc
        self._fernet = fernet

    def lock(self) -> None:
        self._fernet = None
        self._data = {}

    def _flush(self) -> None:
        if self._fernet is None:
            raise VaultLocked()
        blob = self._fernet.encrypt(json.dumps(self._data).encode("utf-8"))
        envelope = {
            "version": 1,
            "kdf": "scrypt",
            "salt": base64.b64encode(self._salt).decode("ascii"),
            "data": base64.b64encode(blob).decode("ascii"),
        }
        # Write-then-rename so a crash mid-write can't truncate the vault.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - not supported on all filesystems
            pass

    # ── SecretBackend ─────────────────────────────────────────────────────────

    @staticmethod
    def _key(profile_id: str, field: str) -> str:
        return f"{profile_id}:{field}"

    def get(self, profile_id: str, field: str) -> str:
        if self._fernet is None:
            raise VaultLocked()
        return self._data.get(self._key(profile_id, field), "")

    def set(self, profile_id: str, field: str, value: str) -> None:
        if self._fernet is None:
            raise VaultLocked()
        key = self._key(profile_id, field)
        if value:
            self._data[key] = value
        else:
            self._data.pop(key, None)
        self._flush()

    def delete(self, profile_id: str, field: str) -> None:
        if self._fernet is None:
            return
        if self._data.pop(self._key(profile_id, field), None) is not None:
            self._flush()


# ─── Facade ───────────────────────────────────────────────────────────────────

class CredentialStore:
    """Front door for secrets. Picks the best available backend automatically."""

    def __init__(self, backend: SecretBackend | None = None) -> None:
        self._backend = backend or self._auto_select()

    @staticmethod
    def _auto_select() -> SecretBackend:
        try:
            backend = KeyringBackend()
            log.info("Using credential backend: %s", backend.display_name)
            return backend
        except Exception as exc:  # noqa: BLE001
            log.warning("OS keychain unavailable (%s); secrets will not persist "
                        "until a vault is unlocked.", exc)
            return NullBackend()

    # ── backend management ────────────────────────────────────────────────────

    @property
    def backend(self) -> SecretBackend:
        return self._backend

    @property
    def backend_name(self) -> str:
        return self._backend.display_name

    @property
    def is_persistent(self) -> bool:
        """False when secrets are being discarded (NullBackend or locked vault)."""
        return not isinstance(self._backend, NullBackend) and self._backend.is_unlocked

    @property
    def needs_master_password(self) -> bool:
        """True when the only option is a vault the user must unlock."""
        return isinstance(self._backend, NullBackend)

    def use_vault(self, master_password: str) -> None:
        """Switch to (and unlock or create) the encrypted file vault."""
        vault = EncryptedFileBackend()
        vault.unlock(master_password)
        self._backend = vault
        log.info("Using credential backend: %s", vault.display_name)

    def vault_exists(self) -> bool:
        return EncryptedFileBackend().exists

    # ── per-profile access ────────────────────────────────────────────────────

    def load_secrets(self, profile_id: str) -> dict[str, str]:
        """Read every known secret for a profile. Missing values come back ''."""
        out: dict[str, str] = {}
        for field in SECRET_FIELDS:
            try:
                out[field] = self._backend.get(profile_id, field)
            except VaultLocked:
                raise
            except CredentialError:
                out[field] = ""
        return out

    def save_secrets(self, profile_id: str, secrets_map: dict[str, str]) -> None:
        """Persist the given fields. Empty values delete the stored entry."""
        for field, value in secrets_map.items():
            if field not in SECRET_FIELDS:
                continue
            self._backend.set(profile_id, field, value or "")

    def forget(self, profile_id: str) -> None:
        self._backend.delete_all(profile_id)
