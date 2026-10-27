"""Database engine shared by the API (and later by jobs).

SQLModel sits on SQLAlchemy 2.0 and Pydantic v2. A SQLModel class is a Pydantic
model and a database table at the same time, so you define each table once.
We are not creating tables in this phase; this module only opens the connection.

create_engine does not connect yet. The first query opens a connection.
"""

from sqlmodel import create_engine

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 3},
)
