"""The worker against real Postgres and Redis: normal flow and every recovery path."""
from __future__ import annotations

from datetime import timedelta

from judgebox import db, jobs
from judgebox.judge.core import Outcome
from judgebox.worker import main as worker_module
from judgebox.worker.main import Worker
from tests.conftest import PROBLEMS, needs_root

pytestmark = needs_root

AC_SOURCE = "a, b = map(int, input().split())\nprint(a + b)"


def make_submission(sessions, source=AC_SOURCE, created_ago_s=0):
    with sessions() as s:
        if not s.get(db.Problem, "a-plus-b"):
            s.add(db.Problem(slug="a-plus-b", title="A + B", time_limit_ms=1000, memory_mb=64, tests=6))
        user = db.User(username=f"u{db.utcnow().timestamp()}", password_hash="x")
        s.add(user)
        s.flush()
        sub = db.Submission(user_id=user.id, problem_slug="a-plus-b", language="python3", source=source,
                            created_at=db.utcnow() - timedelta(seconds=created_ago_s))
        s.add(sub)
        s.commit()
        return sub.id


def get(sessions, sid):
    with sessions() as s:
        return s.get(db.Submission, sid)


def test_judges_a_submission_and_publishes_events(services):
    sessions, r = services
    sid = make_submission(sessions)
    jobs.enqueue(r, sid)

    assert Worker(r, sessions, PROBLEMS, "w1").step() == 1

    sub = get(sessions, sid)
    assert (sub.status, sub.verdict, sub.attempts) == ("done", "AC", 1)
    assert len(sub.tests) == 6
    kinds = [e["type"] for e in jobs.replay(r, sid)]
    assert kinds[0] == "compiling" and kinds[-1] == "done"
    assert [e["seq"] for e in jobs.replay(r, sid)] == list(range(1, len(kinds) + 1))
    assert jobs.depth(r) == {"waiting": 0, "in_progress": 0}


def test_duplicate_delivery_is_judged_once(services):
    sessions, r = services
    sid = make_submission(sessions)
    jobs.enqueue(r, sid)
    jobs.enqueue(r, sid)
    w = Worker(r, sessions, PROBLEMS, "w1")
    w.step()
    w.step()
    sub = get(sessions, sid)
    assert sub.verdict == "AC" and sub.attempts == 1
    assert sum(e["type"] == "done" for e in jobs.replay(r, sid)) == 1


def test_crashed_worker_message_is_reclaimed(services, monkeypatch):
    sessions, r = services
    sid = make_submission(sessions)
    jobs.enqueue(r, sid)
    # A worker reads the message and dies before acknowledging it.
    r.xreadgroup(jobs.GROUP, "crashed-worker", {jobs.STREAM: ">"}, count=1)
    assert jobs.depth(r)["in_progress"] == 1

    monkeypatch.setattr(worker_module, "VISIBILITY_MS", 0)
    Worker(r, sessions, PROBLEMS, "w2").step()

    assert get(sessions, sid).verdict == "AC"
    assert jobs.depth(r) == {"waiting": 0, "in_progress": 0}


def test_poison_message_goes_to_dead_letter(services, monkeypatch):
    sessions, r = services
    sid = make_submission(sessions)
    msg_id = jobs.enqueue(r, sid)
    r.xreadgroup(jobs.GROUP, "a", {jobs.STREAM: ">"}, count=1)
    for name in ("b", "c", "d"):  # three more workers claimed it and died
        r.xclaim(jobs.STREAM, jobs.GROUP, name, min_idle_time=0, message_ids=[msg_id])

    monkeypatch.setattr(worker_module, "VISIBILITY_MS", 0)
    Worker(r, sessions, PROBLEMS, "w3").step()

    sub = get(sessions, sid)
    assert (sub.status, sub.verdict) == ("done", "SE")
    assert r.xlen(jobs.DEAD) == 1
    assert jobs.depth(r)["in_progress"] == 0


def test_system_error_is_retried_then_stored(services, monkeypatch):
    sessions, r = services
    sid = make_submission(sessions)
    jobs.enqueue(r, sid)
    calls = []

    def broken_judge(*args, **kwargs):
        calls.append(1)
        return Outcome("SE", message="sandbox exploded")

    monkeypatch.setattr(worker_module, "judge", broken_judge)
    w = Worker(r, sessions, PROBLEMS, "w4")
    for _ in range(5):
        w.step()

    sub = get(sessions, sid)
    assert len(calls) == worker_module.MAX_ATTEMPTS
    assert (sub.status, sub.verdict, sub.attempts) == ("done", "SE", 3)
    assert [e["type"] for e in jobs.replay(r, sid)].count("retrying") == 2


def test_time_limit_is_not_retried(services):
    sessions, r = services
    sid = make_submission(sessions, source="while True: pass")
    jobs.enqueue(r, sid)
    w = Worker(r, sessions, PROBLEMS, "w5")
    w.step()
    w.step()
    sub = get(sessions, sid)
    assert (sub.verdict, sub.attempts) == ("TLE", 1)


def test_lost_enqueue_is_recovered_by_the_sweeper(services):
    sessions, r = services
    sid = make_submission(sessions, created_ago_s=600)  # committed, never reached Redis
    w = Worker(r, sessions, PROBLEMS, "w6")
    assert w.requeue_lost() == 1
    w.step()
    assert get(sessions, sid).verdict == "AC"


def test_sweeper_leaves_a_busy_queue_alone(services):
    sessions, r = services
    old = make_submission(sessions, created_ago_s=600)
    jobs.enqueue(r, make_submission(sessions))  # something is waiting
    assert Worker(r, sessions, PROBLEMS, "w7").requeue_lost() == 0
    assert get(sessions, old).status == "queued"


def test_finish_refuses_to_overwrite(services):
    sessions, r = services
    sid = make_submission(sessions)
    w = Worker(r, sessions, PROBLEMS, "w8")
    assert w.finish(sid, Outcome("AC")) is True
    assert w.finish(sid, Outcome("WA")) is False
    assert get(sessions, sid).verdict == "AC"
