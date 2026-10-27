"""Database engine shared by the API (and later by jobs).

SQLModel sits on SQLAlchemy 2.0 and Pydantic v2. A SQLModel class is a Pydantic
model and a database table at the same time, so you define each table once.
We are not creating tables in this phase; this module only opens the connection.

create_engine does not connect yet. The first query opens a connection.
"""

from sqlalchemy import MetaData
from sqlmodel import SQLModel, create_engine

from app.config import get_settings

SQLModel.metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 3},
)
