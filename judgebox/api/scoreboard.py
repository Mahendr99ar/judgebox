"""ICPC-style scoreboard, computed from submissions on every request.

Rules:
* rank by problems solved, then total penalty, then time of the last solve
* penalty for a solved problem = minutes from contest start to the first AC
  + penalty_minutes for each earlier rejected attempt on that problem
* CE and SE never count as attempts; attempts after the first AC are ignored
* during the freeze, other contestants' submissions show only as pending
  attempts until an admin unfreezes the board

Computing it from rows keeps one source of truth. For a contest of a few
hundred people this is one indexed query; see docs/decisions.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from judgebox import db

NOT_ATTEMPTS = {"CE", "SE"}


@dataclass
class Cell:
    solved: bool = False
    minute: int | None = None
    rejected: int = 0
    pending: int = 0


@dataclass
class Row:
    user_id: int
    username: str
    solved: int = 0
    penalty: int = 0
    last_solve: int = 0
    rank: int = 0
    cells: dict[str, Cell] = field(default_factory=dict)


def compute(session, contest: db.Contest, viewer: db.User | None, now: datetime) -> dict:
    problems = session.execute(
        select(db.ContestProblem.problem_slug, db.ContestProblem.label)
        .where(db.ContestProblem.contest_id == contest.id)
        .order_by(db.ContestProblem.label)
    ).all()
    labels = {slug: label for slug, label in problems}

    people = session.execute(
        select(db.User.id, db.User.username)
        .join(db.Participant, db.Participant.user_id == db.User.id)
        .where(db.Participant.contest_id == contest.id)
    ).all()
    rows = {uid: Row(uid, name, cells={label: Cell() for label in labels.values()}) for uid, name in people}

    hidden_after = None
    is_admin = viewer is not None and viewer.role == "admin"
    if contest.freeze_at and not contest.unfrozen and not is_admin and now >= contest.freeze_at:
        hidden_after = contest.freeze_at

    subs = session.execute(
        select(db.Submission.user_id, db.Submission.problem_slug, db.Submission.status,
               db.Submission.verdict, db.Submission.created_at)
        .where(db.Submission.contest_id == contest.id,
               db.Submission.created_at >= contest.starts_at,
               db.Submission.created_at < contest.ends_at)
        .order_by(db.Submission.created_at, db.Submission.id)
    ).all()

    for user_id, slug, status, verdict, created_at in subs:
        row = rows.get(user_id)
        if row is None or slug not in labels:
            continue
        cell = row.cells[labels[slug]]
        if cell.solved or verdict in NOT_ATTEMPTS:
            continue
        own = viewer is not None and viewer.id == user_id
        if status != "done" or (hidden_after and created_at >= hidden_after and not own):
            cell.pending += 1
            continue
        if verdict == "AC":
            cell.solved = True
            cell.minute = int((created_at - contest.starts_at).total_seconds() // 60)
        else:
            cell.rejected += 1

    for row in rows.values():
        for cell in row.cells.values():
            if cell.solved:
                row.solved += 1
                row.penalty += cell.minute + contest.penalty_minutes * cell.rejected
                row.last_solve = max(row.last_solve, cell.minute)

    ordered = sorted(rows.values(), key=lambda r: (-r.solved, r.penalty, r.last_solve, r.username))
    prev, rank = None, 0
    for i, row in enumerate(ordered, 1):
        key = (row.solved, row.penalty, row.last_solve)
        if key != prev:
            rank, prev = i, key
        row.rank = rank

    return {
        "contest": contest.slug,
        "frozen": hidden_after is not None,
        "problems": [{"label": label, "slug": slug} for slug, label in problems],
        "rows": [
            {
                "rank": r.rank,
                "username": r.username,
                "solved": r.solved,
                "penalty": r.penalty,
                "cells": {k: vars(v) for k, v in r.cells.items()},
            }
            for r in ordered
        ],
    }
