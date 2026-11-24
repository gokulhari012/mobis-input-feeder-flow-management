"""Authentication, role based permissions and user administration.

When operator login is not required, the station runs as a shared
"Station Operator" session with OPERATOR permissions; supervisors and
administrators log in on demand (elevation) and are logged out automatically
after the configured inactivity timeout.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

from mobis.core.permissions import Permission, Role, role_has
from mobis.core.security import hash_password, password_problem, verify_password
from mobis.database.database import Database
from mobis.database.repositories import Repositories
from mobis.utils.timeutil import fmt_datetime, utc_now

from .audit_service import AuditService
from .configuration_service import ConfigurationService

log = logging.getLogger("mobis.auth")

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 5


@dataclass(frozen=True)
class SessionUser:
    username: str
    full_name: str
    role: Role
    user_id: Optional[int] = None
    is_station: bool = False

    @property
    def display(self) -> str:
        return self.full_name or self.username


STATION_USER = SessionUser("", "Station Operator", Role.OPERATOR, None, True)


class AuthService(QObject):
    user_changed = Signal()
    session_expired = Signal()

    def __init__(self, db: Database, repos: Repositories, audit: AuditService, config: ConfigurationService):
        super().__init__()
        self._db = db
        self._repos = repos
        self._audit = audit
        self._config = config
        self._user = STATION_USER
        self._last_activity = time.monotonic()

    # ------------------------------------------------------------------ session
    @property
    def current_user(self) -> SessionUser:
        return self._user

    def record_username(self) -> str:
        """Username stored on transactions ('' for the shared station session)."""
        return "" if self._user.is_station else self._user.username

    def audit_username(self) -> str:
        return self._user.username or "station"

    def has_permission(self, permission: Permission) -> bool:
        return role_has(self._user.role, permission)

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def check_timeout(self) -> bool:
        """Log out an elevated (supervisor / admin) session after inactivity."""
        if self._user.is_station or self._user.role == Role.OPERATOR:
            return False
        minutes = self._config.app().admin_session_timeout_min
        if minutes <= 0 or time.monotonic() - self._last_activity < minutes * 60:
            return False
        self.logout(reason="session timeout")
        self.session_expired.emit()
        return True

    def login(self, username: str, password: str) -> tuple[bool, str, bool]:
        """Returns ``(success, message, must_change_password)``."""
        ok, message, user, must_change = self._authenticate(username, password)
        if not ok:
            return False, message, False
        self._user = user
        self.touch()
        self._audit.log("SECURITY", "LOGIN", f"User '{user.username}' logged in ({user.role.value})",
                        entity="user", entity_id=user.user_id, username=user.username)
        self.user_changed.emit()
        return True, "", must_change

    def verify_credentials(
        self, username: str, password: str, permission: Permission
    ) -> tuple[bool, str, Optional[SessionUser]]:
        """Check credentials for a one-off authorisation without changing the session."""
        ok, message, user, _must = self._authenticate(username, password)
        if not ok or user is None:
            return False, message, None
        if not role_has(user.role, permission):
            return False, f"User '{user.username}' is not authorised for this action.", None
        return True, "", user

    def _authenticate(self, username: str, password: str):
        username = (username or "").strip()
        now = utc_now()
        with self._db.session() as s:
            row = self._repos.users.find_by_username(s, username)
            if row is None or not row.is_active:
                self._audit.log("SECURITY", "LOGIN_FAILED", f"Unknown or inactive user '{username}'",
                                username=username, session=s)
                return False, "Invalid username or password.", None, False
            if row.locked_until and row.locked_until > now:
                return False, f"Account locked until {fmt_datetime(row.locked_until)}.", None, False
            if not verify_password(password or "", row.password_hash):
                row.failed_attempts = (row.failed_attempts or 0) + 1
                message = "Invalid username or password."
                if row.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    row.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                    row.failed_attempts = 0
                    message = f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
                self._audit.log("SECURITY", "LOGIN_FAILED", f"Wrong password for '{row.username}'",
                                entity="user", entity_id=row.id, username=row.username, session=s)
                return False, message, None, False
            row.failed_attempts = 0
            row.locked_until = None
            row.last_login_at = now
            user = SessionUser(row.username, row.full_name, Role(row.role), row.id)
            return True, "", user, bool(row.must_change_password)

    def logout(self, reason: str = "logout") -> None:
        if self._user.is_station:
            return
        self._audit.log("SECURITY", "LOGOUT", f"User '{self._user.username}' logged out ({reason})",
                        entity="user", entity_id=self._user.user_id, username=self._user.username)
        self._user = STATION_USER
        self.user_changed.emit()

    # ------------------------------------------------------------------ passwords
    def change_password(self, username: str, old_password: str, new_password: str) -> tuple[bool, str]:
        problem = password_problem(new_password)
        if problem:
            return False, problem
        if old_password == new_password:
            return False, "The new password must be different from the current password."
        with self._db.session() as s:
            row = self._repos.users.find_by_username(s, username)
            if row is None or not verify_password(old_password, row.password_hash):
                return False, "Current password is incorrect."
            row.password_hash = hash_password(new_password)
            row.must_change_password = False
            row.updated_at = utc_now()
            self._audit.log("SECURITY", "PASSWORD_CHANGED", f"User '{row.username}' changed password",
                            entity="user", entity_id=row.id, username=row.username, session=s)
        return True, ""

    def admin_set_password(self, user_id: int, new_password: str, must_change: bool = True) -> tuple[bool, str]:
        problem = password_problem(new_password)
        if problem:
            return False, problem
        with self._db.session() as s:
            row = self._repos.users.get(s, user_id)
            if row is None:
                return False, "User not found."
            self._repos.users.update(s, user_id, password_hash=hash_password(new_password),
                                     must_change_password=must_change, failed_attempts=0, locked_until=None)
            self._audit.log("USER", "PASSWORD_RESET", f"Password reset for '{row.username}'",
                            entity="user", entity_id=user_id, username=self.audit_username(), session=s)
        return True, ""

    # ------------------------------------------------------------------ user admin
    def list_users(self) -> list[dict[str, Any]]:
        with self._db.read_session() as s:
            return [
                {
                    "id": u.id,
                    "username": u.username,
                    "full_name": u.full_name,
                    "role": u.role,
                    "is_active": bool(u.is_active),
                    "must_change_password": bool(u.must_change_password),
                    "last_login_at": u.last_login_at,
                    "locked_until": u.locked_until,
                }
                for u in self._repos.users.list(s)
            ]

    def create_user(
        self, username: str, full_name: str, role: Role, password: str, must_change: bool = True
    ) -> tuple[bool, str]:
        username = (username or "").strip()
        if not username or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
            return False, "Username is required (letters, digits, '.', '_' and '-' only)."
        problem = password_problem(password)
        if problem:
            return False, problem
        with self._db.session() as s:
            if self._repos.users.find_by_username(s, username):
                return False, f"User '{username}' already exists."
            row = self._repos.users.create(
                s, username=username, full_name=full_name.strip(), role=Role(role).value,
                password_hash=hash_password(password), is_active=True,
                must_change_password=must_change, failed_attempts=0,
            )
            self._audit.log("USER", "CREATE", f"User '{username}' created with role {Role(role).value}",
                            entity="user", entity_id=row.id, new={"role": Role(role).value, "full_name": full_name},
                            username=self.audit_username(), session=s)
        return True, ""

    def update_user(self, user_id: int, full_name: str, role: Role, is_active: bool) -> tuple[bool, str]:
        with self._db.session() as s:
            row = self._repos.users.get(s, user_id)
            if row is None:
                return False, "User not found."
            demoting_admin = row.role == Role.ADMIN.value and (Role(role) != Role.ADMIN or not is_active)
            if demoting_admin and self._repos.users.count_active_admins(s, exclude_id=user_id) == 0:
                return False, "At least one active administrator account is required."
            old = {"full_name": row.full_name, "role": row.role, "is_active": bool(row.is_active)}
            self._repos.users.update(s, user_id, full_name=full_name.strip(), role=Role(role).value,
                                     is_active=is_active)
            self._audit.log("USER", "UPDATE", f"User '{row.username}' updated", entity="user", entity_id=user_id,
                            old=old, new={"full_name": full_name, "role": Role(role).value, "is_active": is_active},
                            username=self.audit_username(), session=s)
        return True, ""
