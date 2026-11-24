"""Versioned schema migrations.

To change the schema in a future version:

1. Add a module ``mNNNN_description.py`` with ``VERSION``, ``NAME`` and
   ``STATEMENTS`` (plain SQLite DDL, e.g. ``ALTER TABLE ... ADD COLUMN ...``).
2. Append the module name to ``MIGRATION_MODULES`` in :mod:`.runner`.
3. Update the ORM classes in :mod:`mobis.database.models`.

Never edit a migration that has already been released - existing plant
databases have already applied it.  The runner backs up the database before
applying migrations to an existing installation.
"""

from .runner import MigrationRunner, SchemaVersionError, load_migrations

__all__ = ["MigrationRunner", "SchemaVersionError", "load_migrations"]
