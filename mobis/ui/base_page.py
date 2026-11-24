"""Base class of all navigation pages."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget


class BasePage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("Page")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 16, 20, 16)
        self.root.setSpacing(12)

    def on_show(self) -> None:
        """Called every time the page becomes the current page."""

    def on_hide(self) -> None:
        """Called when another page becomes current."""

    def can_leave(self) -> bool:
        """Return False to keep the user on the page (e.g. unsaved changes declined)."""
        return True

    def has_unsaved_changes(self) -> bool:
        """True when leaving would lose edits (checked without asking the user)."""
        from mobis.ui.widgets.config_form import ConfigForm

        return any(form.is_dirty() for form in self.findChildren(ConfigForm))

    def on_user_changed(self) -> None:
        """Called after login / logout (permissions may have changed)."""

    @property
    def username(self) -> str:
        return self.ctx.auth.audit_username()
