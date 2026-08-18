from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative root whose metadata is the Alembic autogenerate target.

    Alembic owns every schema object; nothing may call ``Base.metadata.create_all``.
    """
