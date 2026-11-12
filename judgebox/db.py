from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from judgebox import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    # Only the create-admin command sets "admin"; the signup endpoint cannot.
    role: Mapped[str] = mapped_column(String(10), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Problem(Base):
    __tablename__ = "problems"
    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    time_limit_ms: Mapped[int] = mapped_column(Integer)
    memory_mb: Mapped[int] = mapped_column(Integer)
    tests: Mapped[int] = mapped_column(Integer)
    # Hidden problems can only be used inside contests until they are published.
    public: Mapped[bool] = mapped_column(Boolean, default=True)


class Contest(Base):
    __tablename__ = "contests"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    freeze_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unfrozen: Mapped[bool] = mapped_column(Boolean, default=False)
    penalty_minutes: Mapped[int] = mapped_column(Integer, default=20)


class ContestProblem(Base):
    __tablename__ = "contest_problems"
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), primary_key=True)
    problem_slug: Mapped[str] = mapped_column(ForeignKey("problems.slug"), primary_key=True)
    label: Mapped[str] = mapped_column(String(4))


class Participant(Base):
    __tablename__ = "participants"
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Submission(Base):
    """One row per submission. The contest scoreboard is computed from these
    rows, never kept as a separate counter that two workers could race on."""

    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    problem_slug: Mapped[str] = mapped_column(ForeignKey("problems.slug"))
    contest_id: Mapped[int | None] = mapped_column(ForeignKey("contests.id"), nullable=True)
    language: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued, running, done
    verdict: Mapped[str | None] = mapped_column(String(4), nullable=True)
    cpu_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    compile_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tests: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_submissions_scoreboard", "contest_id", "user_id", "problem_slug", "created_at"),
        Index("ix_submissions_user", "user_id", "created_at"),
        Index("ix_submissions_status", "status", "created_at"),
    )


@lru_cache(maxsize=1)
def engine():
    return create_engine(settings.get().database_url, pool_pre_ping=True, pool_size=10)


@lru_cache(maxsize=1)
def session_factory():
    return sessionmaker(engine(), expire_on_commit=False)


def create_schema() -> None:
    Base.metadata.create_all(engine())
