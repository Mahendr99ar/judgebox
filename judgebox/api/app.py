"""HTTP API and the static web UI.

    uvicorn judgebox.api.app:app

Submitting returns 202 with an id right away; judging happens in the
workers. The page follows progress over Server-Sent Events from
/api/submissions/{id}/events.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import redis
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from judgebox import db, jobs, settings
from judgebox.api import scoreboard
from judgebox.api.auth import admin_user, check_password, current_user, hash_password, issue_token
from judgebox.judge import problem as problems
from judgebox.judge.languages import LANGUAGES

WEB = Path(__file__).resolve().parents[2] / "web"
SSE_MAX_SECONDS = 300


class Strict(BaseModel):
    # Unknown fields are an error, so nobody can slip {"role": "admin"} into
    # a signup request and have it silently accepted somewhere later.
    model_config = ConfigDict(extra="forbid")


class Credentials(Strict):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class NewSubmission(Strict):
    problem: str
    language: str
    source: str
    contest: str | None = None


class NewContest(Strict):
    slug: str = Field(pattern=r"^[a-z0-9-]{3,64}$")
    title: str
    starts_at: datetime
    ends_at: datetime
    freeze_at: datetime | None = None
    penalty_minutes: int = 20
    problems: list[str] = Field(min_length=1, max_length=26)


def sync_problems() -> int:
    """Mirror the problem packages on disk into the problems table."""
    found = problems.load_all(settings.get().problems_dir)
    with db.session_factory()() as s:
        for p in found.values():
            row = s.get(db.Problem, p.slug) or db.Problem(slug=p.slug)
            row.title, row.time_limit_ms, row.memory_mb = p.title, p.time_limit_ms, p.memory_mb
            row.tests, row.public = len(p.tests), p.public
            s.merge(row)
        s.commit()
    return len(found)


def create_app() -> FastAPI:
    cfg = settings.get()
    r = jobs.client(cfg.redis_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.create_schema()
        sync_problems()
        jobs.ensure_group(r)
        yield

    app = FastAPI(title="judgebox", lifespan=lifespan)
    sessions = db.session_factory()

    # ------------------------------------------------------------ accounts

    @app.post("/api/auth/register", status_code=201)
    def register(body: Credentials):
        with sessions() as s:
            s.add(db.User(username=body.username, password_hash=hash_password(body.password)))
            try:
                s.commit()
            except IntegrityError:
                raise HTTPException(status.HTTP_409_CONFLICT, "username taken") from None
        return {"username": body.username}

    @app.post("/api/auth/login")
    def login(body: Credentials):
        with sessions() as s:
            user = s.scalar(select(db.User).where(db.User.username == body.username))
        if user is None or not check_password(body.password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
        return {"token": issue_token(user), "username": user.username, "role": user.role}

    @app.get("/api/me")
    def me(user: db.User = Depends(current_user)):
        return {"id": user.id, "username": user.username, "role": user.role}

    # ------------------------------------------------------------ problems

    @app.get("/api/languages")
    def languages():
        return [{"key": lang.key, "name": lang.name} for lang in LANGUAGES.values()]

    @app.get("/api/problems")
    def list_problems():
        with sessions() as s:
            rows = s.scalars(select(db.Problem).where(db.Problem.public).order_by(db.Problem.slug)).all()
        return [{"slug": p.slug, "title": p.title, "time_limit_ms": p.time_limit_ms,
                 "memory_mb": p.memory_mb} for p in rows]

    @app.get("/api/problems/{slug}")
    def get_problem(slug: str):
        with sessions() as s:
            row = s.get(db.Problem, slug)
            visible = row is not None and (row.public or _in_started_contest(s, slug))
        if not visible:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such problem")
        p = problems.load(settings.get().problems_dir / slug)
        return {"slug": p.slug, "title": p.title, "time_limit_ms": p.time_limit_ms,
                "memory_mb": p.memory_mb, "statement": p.statement, "tests": len(p.tests)}

    # ------------------------------------------------------------ submissions

    @app.post("/api/submissions", status_code=202)
    def submit(body: NewSubmission, user: db.User = Depends(current_user)):
        if body.language not in LANGUAGES:
            raise HTTPException(422, f"language must be one of {sorted(LANGUAGES)}")
        if len(body.source.encode()) > cfg.max_source_kb * 1024:
            raise HTTPException(413, f"source is larger than {cfg.max_source_kb} KB")
        _rate_limit(r, user.id, cfg.submit_per_minute)

        with sessions() as s:
            prob = s.get(db.Problem, body.problem)
            contest_id = None
            if body.contest:
                contest = s.scalar(select(db.Contest).where(db.Contest.slug == body.contest))
                contest_id = _check_contest_submission(s, contest, user, body.problem)
            elif prob is None or not prob.public:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such problem")
            sub = db.Submission(user_id=user.id, problem_slug=body.problem, contest_id=contest_id,
                                language=body.language, source=body.source)
            s.add(sub)
            s.commit()
            sid = sub.id
        try:
            jobs.enqueue(r, sid)
        except redis.RedisError:
            # The row is committed; the worker's sweeper will enqueue it.
            pass
        return {"id": sid, "status": "queued"}

    @app.get("/api/submissions")
    def my_submissions(user: db.User = Depends(current_user), limit: int = 50):
        with sessions() as s:
            rows = s.scalars(select(db.Submission).where(db.Submission.user_id == user.id)
                             .order_by(db.Submission.id.desc()).limit(min(limit, 200))).all()
        return [_submission_summary(x) for x in rows]

    @app.get("/api/submissions/{sid}")
    def get_submission(sid: int, user: db.User = Depends(current_user)):
        sub = _visible_submission(sid, user)
        return {**_submission_summary(sub), "source": sub.source, "compile_message": sub.compile_message,
                "tests": sub.tests or []}

    @app.get("/api/submissions/{sid}/events")
    async def submission_events(sid: int, request: Request, user: db.User = Depends(current_user)):
        sub = await run_in_threadpool(_visible_submission, sid, user)
        return StreamingResponse(_event_stream(cfg.redis_url, sub, request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ contests

    @app.get("/api/contests")
    def list_contests():
        with sessions() as s:
            rows = s.scalars(select(db.Contest).order_by(db.Contest.starts_at.desc())).all()
        return [_contest_summary(c) for c in rows]

    @app.get("/api/contests/{slug}")
    def get_contest(slug: str):
        with sessions() as s:
            c = _contest_or_404(s, slug)
            items = []
            if db.utcnow() >= c.starts_at:  # do not reveal problems early
                items = [{"label": label, "slug": p, "title": title} for p, label, title in s.execute(
                    select(db.ContestProblem.problem_slug, db.ContestProblem.label, db.Problem.title)
                    .join(db.Problem).where(db.ContestProblem.contest_id == c.id)
                    .order_by(db.ContestProblem.label))]
        return {**_contest_summary(c), "problems": items}

    @app.post("/api/contests/{slug}/join", status_code=201)
    def join_contest(slug: str, user: db.User = Depends(current_user)):
        with sessions() as s:
            c = _contest_or_404(s, slug)
            if db.utcnow() >= c.ends_at:
                raise HTTPException(status.HTTP_409_CONFLICT, "contest is over")
            if not s.get(db.Participant, (c.id, user.id)):
                s.add(db.Participant(contest_id=c.id, user_id=user.id))
                s.commit()
        return {"joined": slug}

    @app.get("/api/contests/{slug}/scoreboard")
    def get_scoreboard(slug: str, request: Request):
        viewer = None
        if request.headers.get("authorization") or request.query_params.get("token"):
            viewer = current_user(request)
        with sessions() as s:
            c = _contest_or_404(s, slug)
            return scoreboard.compute(s, c, viewer, db.utcnow())

    # ------------------------------------------------------------ admin

    @app.post("/api/admin/contests", status_code=201)
    def create_contest(body: NewContest, _: db.User = Depends(admin_user)):
        if not body.starts_at < body.ends_at:
            raise HTTPException(422, "contest must end after it starts")
        if body.freeze_at and not body.starts_at <= body.freeze_at <= body.ends_at:
            raise HTTPException(422, "freeze time must be inside the contest")
        with sessions() as s:
            missing = [p for p in body.problems if s.get(db.Problem, p) is None]
            if missing:
                raise HTTPException(422, f"unknown problems: {missing}")
            c = db.Contest(slug=body.slug, title=body.title, starts_at=body.starts_at, ends_at=body.ends_at,
                           freeze_at=body.freeze_at, penalty_minutes=body.penalty_minutes)
            s.add(c)
            try:
                s.flush()
            except IntegrityError:
                raise HTTPException(status.HTTP_409_CONFLICT, "contest slug taken") from None
            for i, slug in enumerate(body.problems):
                s.add(db.ContestProblem(contest_id=c.id, problem_slug=slug, label=chr(ord("A") + i)))
            s.commit()
        return {"slug": body.slug}

    @app.post("/api/admin/contests/{slug}/unfreeze")
    def unfreeze(slug: str, _: db.User = Depends(admin_user)):
        with sessions() as s:
            c = _contest_or_404(s, slug)
            c.unfrozen = True
            s.commit()
        return {"unfrozen": slug}

    @app.post("/api/admin/problems/sync")
    def resync(_: db.User = Depends(admin_user)):
        return {"problems": sync_problems()}

    @app.get("/api/admin/queue")
    def queue_status(_: db.User = Depends(admin_user)):
        with sessions() as s:
            counts = dict(s.execute(select(db.Submission.status, func.count()).group_by(db.Submission.status)).all())
        return {"stream": jobs.depth(r), "dead_letters": r.xlen(jobs.DEAD), "submissions": counts}

    @app.get("/api/health")
    def health():
        with sessions() as s:
            s.execute(text("select 1"))
        r.ping()
        return {"ok": True, **jobs.depth(r)}

    # ------------------------------------------------------------ web UI

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB / "index.html")

    return app


# ---------------------------------------------------------------- helpers

def _rate_limit(r: redis.Redis, user_id: int, per_minute: int) -> None:
    key = f"jb:rate:{user_id}:{int(db.utcnow().timestamp() // 60)}"
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 120)
    count, _ = pipe.execute()
    if count > per_minute:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"at most {per_minute} submissions per minute")


def _check_contest_submission(s, contest, user, problem_slug) -> int:
    if contest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such contest")
    now = db.utcnow()
    if not contest.starts_at <= now < contest.ends_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "contest is not running")
    if not s.get(db.Participant, (contest.id, user.id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "join the contest first")
    if not s.get(db.ContestProblem, (contest.id, problem_slug)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "problem is not in this contest")
    return contest.id


def _in_started_contest(s, slug: str) -> bool:
    return s.scalar(
        select(func.count()).select_from(db.ContestProblem).join(db.Contest)
        .where(db.ContestProblem.problem_slug == slug, db.Contest.starts_at <= db.utcnow())
    ) > 0


def _visible_submission(sid: int, user: db.User) -> db.Submission:
    with db.session_factory()() as s:
        sub = s.get(db.Submission, sid)
    # 404 rather than 403 for other people's submissions: do not confirm the id exists.
    if sub is None or (sub.user_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such submission")
    return sub


def _submission_summary(sub: db.Submission) -> dict:
    return {
        "id": sub.id, "problem": sub.problem_slug, "language": sub.language, "status": sub.status,
        "verdict": sub.verdict, "cpu_ms": sub.cpu_ms, "memory_kb": sub.memory_kb, "message": sub.message,
        "created_at": sub.created_at.isoformat(),
    }


def _contest_or_404(s, slug: str) -> db.Contest:
    c = s.scalar(select(db.Contest).where(db.Contest.slug == slug))
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such contest")
    return c


def _contest_summary(c: db.Contest) -> dict:
    return {"slug": c.slug, "title": c.title, "starts_at": c.starts_at.isoformat(),
            "ends_at": c.ends_at.isoformat(), "freeze_at": c.freeze_at.isoformat() if c.freeze_at else None}


async def _event_stream(redis_url: str, sub: db.Submission, request: Request):
    """Replay what already happened, then forward live events until 'done'."""
    def frame(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    if sub.status == "done":
        yield frame({"type": "done", "verdict": sub.verdict, "cpu_ms": sub.cpu_ms,
                     "memory_kb": sub.memory_kb, "message": sub.message})
        return

    r = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    try:
        # Subscribe before reading the log, so nothing falls between the two.
        await pubsub.subscribe(jobs.channel(sub.id))
        last = 0
        for raw in await r.lrange(jobs.log_key(sub.id), 0, -1):
            if not raw:
                continue
            event = json.loads(raw)
            last = event["seq"]
            yield frame(event)
            if event["type"] == "done":
                return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + SSE_MAX_SECONDS
        while loop.time() < deadline and not await request.is_disconnected():
            msg = await pubsub.get_message(timeout=15)
            if msg is None:
                yield ": keep-alive\n\n"
                continue
            event = json.loads(msg["data"])
            if event["seq"] <= last:
                continue
            last = event["seq"]
            yield frame(event)
            if event["type"] == "done":
                return
    finally:
        await pubsub.aclose()
        await r.aclose()


app = create_app()
