"""HTTP API, with a real worker judging in-process between requests."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from judgebox import db, jobs
from judgebox.api import scoreboard
from judgebox.api.app import _rate_limit, create_app
from judgebox.api.auth import hash_password
from judgebox.worker.main import Worker
from tests.conftest import PROBLEMS, needs_root

AC = "a, b = map(int, input().split())\nprint(a + b)"


@pytest.fixture
def api(services):
    sessions, r = services
    with TestClient(create_app()) as client:
        yield client, sessions, r


def signup(client, name, password="correct horse"):
    assert client.post("/api/auth/register", json={"username": name, "password": password}).status_code == 201
    token = client.post("/api/auth/login", json={"username": name, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def make_admin(client, sessions, name="root"):
    with sessions() as s:
        s.add(db.User(username=name, password_hash=hash_password("admin password 1"), role="admin"))
        s.commit()
    token = client.post("/api/auth/login", json={"username": name, "password": "admin password 1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- accounts

def test_signup_cannot_choose_a_role(api):
    client, sessions, _ = api
    res = client.post("/api/auth/register", json={"username": "mallory", "password": "password123", "role": "admin"})
    assert res.status_code == 422
    assert client.post("/api/auth/login", json={"username": "mallory", "password": "password123"}).status_code == 401


def test_new_accounts_are_plain_users(api):
    client, _, _ = api
    h = signup(client, "alice")
    assert client.get("/api/me", headers=h).json()["role"] == "user"
    assert client.post("/api/admin/problems/sync", headers=h).status_code == 403


def test_bad_and_missing_tokens_are_rejected(api):
    client, _, _ = api
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


# ---------------------------------------------------------------- submissions

@needs_root
def test_submit_judge_and_read_back(api):
    client, sessions, r = api
    h = signup(client, "alice")
    res = client.post("/api/submissions", headers=h, json={"problem": "a-plus-b", "language": "python3", "source": AC})
    assert res.status_code == 202
    sid = res.json()["id"]
    assert client.get(f"/api/submissions/{sid}", headers=h).json()["status"] == "queued"

    Worker(r, sessions, PROBLEMS, "w").step()

    body = client.get(f"/api/submissions/{sid}", headers=h).json()
    assert (body["status"], body["verdict"]) == ("done", "AC")
    assert len(body["tests"]) == 6

    with client.stream("GET", f"/api/submissions/{sid}/events", headers=h) as stream:
        frames = [line for line in stream.iter_lines() if line.startswith("data:")]
    assert '"verdict": "AC"' in frames[-1]


def test_live_events_replay_then_stop_at_done(api):
    client, sessions, r = api
    h = signup(client, "alice")
    sid = client.post("/api/submissions", headers=h,
                      json={"problem": "a-plus-b", "language": "python3", "source": AC}).json()["id"]
    jobs.publish(r, sid, {"type": "compiling"})
    jobs.publish(r, sid, {"type": "test", "index": 1, "verdict": "AC"})
    jobs.publish(r, sid, {"type": "done", "verdict": "AC"})
    with client.stream("GET", f"/api/submissions/{sid}/events?token={h['Authorization'][7:]}") as stream:
        frames = [line for line in stream.iter_lines() if line.startswith("data:")]
    assert len(frames) == 3 and '"done"' in frames[-1]


def test_other_peoples_submissions_are_invisible(api):
    client, _, _ = api
    alice, bob = signup(client, "alice"), signup(client, "bob")
    sid = client.post("/api/submissions", headers=alice,
                      json={"problem": "a-plus-b", "language": "python3", "source": AC}).json()["id"]
    assert client.get(f"/api/submissions/{sid}", headers=bob).status_code == 404
    assert client.get(f"/api/submissions/{sid}/events", headers=bob).status_code == 404
    assert client.get("/api/submissions", headers=bob).json() == []


def test_submission_validation(api):
    client, _, _ = api
    h = signup(client, "alice")
    post = lambda body: client.post("/api/submissions", headers=h, json=body).status_code  # noqa: E731
    assert post({"problem": "a-plus-b", "language": "brainfuck", "source": "+"}) == 422
    assert post({"problem": "nope", "language": "python3", "source": "x"}) == 404
    assert post({"problem": "a-plus-b", "language": "python3", "source": "x" * 70_000}) == 413
    assert post({"problem": "a-plus-b", "language": "python3", "source": "x", "user_id": 1}) == 422


def test_rate_limit(services):
    _, r = services
    _rate_limit(r, 1, 2)
    _rate_limit(r, 1, 2)
    with pytest.raises(Exception) as exc:
        _rate_limit(r, 1, 2)
    assert exc.value.status_code == 429


# ---------------------------------------------------------------- contests

def _contest(client, admin, **overrides):
    now = db.utcnow()
    body = {"slug": "weekly-1", "title": "Weekly 1", "starts_at": (now - timedelta(minutes=30)).isoformat(),
            "ends_at": (now + timedelta(hours=1)).isoformat(), "problems": ["a-plus-b", "circle-area"]}
    body.update(overrides)
    return client.post("/api/admin/contests", headers=admin, json=body)


def test_contest_submission_rules(api):
    client, sessions, _ = api
    admin = make_admin(client, sessions)
    assert _contest(client, admin).status_code == 201
    alice = signup(client, "alice")
    body = {"problem": "a-plus-b", "language": "python3", "source": AC, "contest": "weekly-1"}

    assert client.post("/api/submissions", headers=alice, json=body).status_code == 403  # not joined
    assert client.post("/api/contests/weekly-1/join", headers=alice).status_code == 201
    assert client.post("/api/submissions", headers=alice, json=body).status_code == 202
    assert client.post("/api/submissions", headers=alice,
                       json={**body, "problem": "two-sum"}).status_code == 404  # not in this contest


def test_contest_problems_hidden_before_start(api):
    client, sessions, _ = api
    admin = make_admin(client, sessions)
    later = db.utcnow() + timedelta(days=1)
    _contest(client, admin, slug="future", starts_at=later.isoformat(),
             ends_at=(later + timedelta(hours=2)).isoformat())
    assert client.get("/api/contests/future").json()["problems"] == []


def _seed_scoreboard(sessions, freeze_minutes=None):
    """alice: A solved at 10 min after 1 WA (and a CE that must not count), B solved at 40.
    bob: A solved at 20 min. carol: one WA. Times are relative to the start."""
    start = db.utcnow() - timedelta(hours=1)
    with sessions() as s:
        for slug in ("a-plus-b", "circle-area"):
            if not s.get(db.Problem, slug):
                s.add(db.Problem(slug=slug, title=slug, time_limit_ms=1000, memory_mb=64, tests=1))
        users = {n: db.User(username=n, password_hash="x") for n in ("alice", "bob", "carol")}
        s.add_all(users.values())
        c = db.Contest(slug="c", title="c", starts_at=start, ends_at=start + timedelta(hours=2),
                       freeze_at=start + timedelta(minutes=freeze_minutes) if freeze_minutes else None)
        s.add(c)
        s.flush()
        s.add_all([db.ContestProblem(contest_id=c.id, problem_slug="a-plus-b", label="A"),
                   db.ContestProblem(contest_id=c.id, problem_slug="circle-area", label="B")])
        s.add_all([db.Participant(contest_id=c.id, user_id=u.id) for u in users.values()])
        for name, slug, verdict, minute in [
            ("alice", "a-plus-b", "CE", 2), ("alice", "a-plus-b", "WA", 5), ("alice", "a-plus-b", "AC", 10),
            ("alice", "a-plus-b", "WA", 12),  # after solving: ignored
            ("bob", "a-plus-b", "AC", 20), ("alice", "circle-area", "AC", 40), ("carol", "a-plus-b", "WA", 50),
        ]:
            s.add(db.Submission(user_id=users[name].id, problem_slug=slug, contest_id=c.id, language="python3",
                                source="", status="done", verdict=verdict,
                                created_at=start + timedelta(minutes=minute, seconds=30)))
        s.commit()
        return c.id, {n: u.id for n, u in users.items()}


def test_icpc_scoring(services):
    sessions, _ = services
    cid, _ = _seed_scoreboard(sessions)
    with sessions() as s:
        board = scoreboard.compute(s, s.get(db.Contest, cid), None, db.utcnow())
    rows = {r["username"]: r for r in board["rows"]}
    # alice: A = 10 + 20 (one WA; CE ignored), B = 40 -> 70
    assert (rows["alice"]["solved"], rows["alice"]["penalty"], rows["alice"]["rank"]) == (2, 70, 1)
    assert (rows["bob"]["solved"], rows["bob"]["penalty"], rows["bob"]["rank"]) == (1, 20, 2)
    assert (rows["carol"]["solved"], rows["carol"]["rank"]) == (0, 3)
    assert rows["alice"]["cells"]["A"] == {"solved": True, "minute": 10, "rejected": 1, "pending": 0}


def test_freeze_hides_others_but_not_yourself_or_admins(services):
    sessions, _ = services
    cid, ids = _seed_scoreboard(sessions, freeze_minutes=30)
    with sessions() as s:
        c = s.get(db.Contest, cid)
        bob = s.get(db.User, ids["bob"])
        as_bob = {r["username"]: r for r in scoreboard.compute(s, c, bob, db.utcnow())["rows"]}
        admin = db.User(id=-1, username="root", password_hash="", role="admin")
        as_admin = {r["username"]: r for r in scoreboard.compute(s, c, admin, db.utcnow())["rows"]}
        c.unfrozen = True
        public = {r["username"]: r for r in scoreboard.compute(s, c, None, db.utcnow())["rows"]}

    # alice's B (minute 40) is after the freeze: bob sees it as pending
    assert as_bob["alice"]["solved"] == 1 and as_bob["alice"]["cells"]["B"]["pending"] == 1
    assert as_admin["alice"]["solved"] == 2
    assert public["alice"]["solved"] == 2
