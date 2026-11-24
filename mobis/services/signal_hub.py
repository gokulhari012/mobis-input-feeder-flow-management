"""Application wide change notifications (decouples pages from each other)."""

from PySide6.QtCore import QObject, Signal


class SignalHub(QObject):
    bag_types_changed = Signal()
    equipment_changed = Signal()
    inventory_changed = Signal()
    transactions_changed = Signal()
    mes_queue_changed = Signal()
