"""Roles and permissions."""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"
    ADMIN = "ADMIN"

    @property
    def label(self) -> str:
        return self.value.title()


class Permission(str, Enum):
    OPERATE = "OPERATE"
    VIEW_HISTORY = "VIEW_HISTORY"
    EXPORT_HISTORY = "EXPORT_HISTORY"
    VIEW_EVENTS = "VIEW_EVENTS"
    VIEW_INVENTORY = "VIEW_INVENTORY"
    CORRECT_INVENTORY = "CORRECT_INVENTORY"
    MANAGE_BAG_TYPES = "MANAGE_BAG_TYPES"
    MANAGE_EQUIPMENT = "MANAGE_EQUIPMENT"
    CONFIGURE_SCANNER = "CONFIGURE_SCANNER"
    CONFIGURE_PLC = "CONFIGURE_PLC"
    PLC_MANUAL_TEST = "PLC_MANUAL_TEST"
    CONFIGURE_MES = "CONFIGURE_MES"
    MES_OPERATIONS = "MES_OPERATIONS"
    MANAGE_USERS = "MANAGE_USERS"
    MANAGE_SYSTEM = "MANAGE_SYSTEM"
    FORCE_COMPLETE = "FORCE_COMPLETE"


_OPERATOR = {Permission.OPERATE, Permission.VIEW_HISTORY, Permission.VIEW_INVENTORY}
_SUPERVISOR = _OPERATOR | {
    Permission.EXPORT_HISTORY,
    Permission.VIEW_EVENTS,
    Permission.MES_OPERATIONS,
    Permission.FORCE_COMPLETE,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.SUPERVISOR: frozenset(_SUPERVISOR),
    Role.ADMIN: frozenset(Permission),
}


def role_has(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
