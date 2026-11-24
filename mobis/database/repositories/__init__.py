"""Repositories: stateless data-access helpers.

Every method takes an explicit SQLAlchemy ``Session`` so that services can
compose several repository operations into one atomic unit of work, e.g.
completing a transaction + updating the tank quantity + queueing the MES record.
"""

from .audit_repository import AuditRepository
from .bag_type_repository import BagTypeRepository
from .config_repository import ConfigRepository
from .equipment_repository import EquipmentRepository
from .inventory_repository import InventoryRepository
from .mes_queue_repository import MesQueueRepository
from .transaction_repository import TransactionFilter, TransactionRepository
from .user_repository import UserRepository


class Repositories:
    """Convenience container holding one instance of every repository."""

    def __init__(self) -> None:
        self.equipment = EquipmentRepository()
        self.bag_types = BagTypeRepository()
        self.transactions = TransactionRepository()
        self.inventory = InventoryRepository()
        self.config = ConfigRepository()
        self.users = UserRepository()
        self.audit = AuditRepository()
        self.mes_queue = MesQueueRepository()


__all__ = [
    "AuditRepository",
    "BagTypeRepository",
    "ConfigRepository",
    "EquipmentRepository",
    "InventoryRepository",
    "MesQueueRepository",
    "Repositories",
    "TransactionFilter",
    "TransactionRepository",
    "UserRepository",
]
