"""Login, password change and one-off authorisation dialogs."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFormLayout, QHBoxLayout, QLineEdit, QVBoxLayout

from mobis.core.permissions import Permission
from mobis.services.auth_service import AuthService, SessionUser

from .widgets.common import button, label


class _BaseDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(20, 18, 20, 16)
        self.layout_.setSpacing(12)
        self.error = label("", "ErrorText", wrap=True)
        self.error.hide()

    def show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def add_buttons(self, ok_text: str, allow_cancel: bool = True):
        row = QHBoxLayout()
        if not allow_cancel:
            exit_button = button("Exit Application")
            exit_button.clicked.connect(QApplication.quit)
            row.addWidget(exit_button)
        row.addStretch(1)
        if allow_cancel:
            cancel = button("Cancel")
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        ok = button(ok_text, "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._on_ok)
        row.addWidget(ok)
        self.layout_.addWidget(self.error)
        self.layout_.addLayout(row)
        return ok

    def _on_ok(self) -> None:  # pragma: no cover - overridden
        self.accept()


class LoginDialog(_BaseDialog):
    def __init__(self, auth: AuthService, parent=None, message: str = "", allow_cancel: bool = True):
        super().__init__("Login", parent)
        self.auth = auth
        self.allow_cancel = allow_cancel
        if message:
            self.layout_.addWidget(label(message, "Muted", wrap=True))
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(label("Username", "FieldLabel"), self.username)
        form.addRow(label("Password", "FieldLabel"), self.password)
        self.layout_.addLayout(form)
        self.add_buttons("Login", allow_cancel)
        self.username.setFocus()

    def reject(self) -> None:
        if self.allow_cancel:
            super().reject()

    def _on_ok(self) -> None:
        username, password = self.username.text().strip(), self.password.text()
        if not username or not password:
            self.show_error("Enter username and password.")
            return
        ok, message, must_change = self.auth.login(username, password)
        if not ok:
            self.show_error(message)
            self.password.clear()
            self.password.setFocus()
            return
        if must_change:
            dialog = ChangePasswordDialog(self.auth, username, old_password=password, forced=True, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.auth.logout("password change required")
                self.show_error("You must set a new password before continuing.")
                return
        self.accept()


class ChangePasswordDialog(_BaseDialog):
    def __init__(self, auth: AuthService, username: str, old_password: Optional[str] = None,
                 forced: bool = False, parent=None):
        super().__init__("Change Password", parent)
        self.auth = auth
        self.username = username
        self.old_password = old_password
        if forced:
            self.layout_.addWidget(label(
                f"The password of '{username}' must be changed before continuing.", "Muted", wrap=True))
        form = QFormLayout()
        self.current = QLineEdit()
        self.current.setEchoMode(QLineEdit.EchoMode.Password)
        self.new = QLineEdit()
        self.new.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        if old_password is None:
            form.addRow(label("Current password", "FieldLabel"), self.current)
        form.addRow(label("New password", "FieldLabel"), self.new)
        form.addRow(label("Repeat new password", "FieldLabel"), self.repeat)
        self.layout_.addLayout(form)
        self.add_buttons("Change Password")

    def _on_ok(self) -> None:
        if self.new.text() != self.repeat.text():
            self.show_error("The new passwords do not match.")
            return
        old = self.old_password if self.old_password is not None else self.current.text()
        ok, message = self.auth.change_password(self.username, old, self.new.text())
        if not ok:
            self.show_error(message)
            return
        self.accept()


class AuthorizeDialog(_BaseDialog):
    """Ask a supervisor / administrator to authorise a single action."""

    def __init__(self, auth: AuthService, permission: Permission, title: str, message: str, parent=None):
        super().__init__(title, parent)
        self.auth = auth
        self.permission = permission
        self.user: Optional[SessionUser] = None
        text = label(message, wrap=True)
        text.setTextFormat(Qt.TextFormat.RichText)
        self.layout_.addWidget(text)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(label("Supervisor / admin", "FieldLabel"), self.username)
        form.addRow(label("Password", "FieldLabel"), self.password)
        self.layout_.addLayout(form)
        self.add_buttons("Authorise")

    def _on_ok(self) -> None:
        ok, message, user = self.auth.verify_credentials(self.username.text(), self.password.text(), self.permission)
        if not ok:
            self.show_error(message)
            self.password.clear()
            return
        self.user = user
        self.accept()
