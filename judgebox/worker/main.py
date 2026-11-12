"""Judge worker: read submissions from the stream, judge, store the verdict.

Run several of these (one per CPU core is a good start). Each has its own
consumer name, so Redis tracks which worker holds which submission.

Failure handling, in the order it can happen:

* worker dies mid-judge: the message stays pending; after VISIBILITY_MS
  another worker claims it with XAUTOCLAIM and judges it again
* a submission crashes workers every time: after MAX_DELIVERIES claims it
  goes to the dead-letter stream and gets verdict SE instead of looping
* sandbox or checker failure (verdict SE): requeued up to MAX_ATTEMPTS, then
  stored as SE. TLE is never retried: a verdict that changes on retry is a
  timing bug, not a feature
* the API committed the row but never reached Redis: the sweeper re-enqueues
  queued rows when the stream is otherwise idle
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from dataclasses import asdict

from sqlalchemy import text

from judgebox import db, jobs, settings
from judgebox.judge import problem as problems
from judgebox.judge.core import Outcome, judge
from judgebox.sandbox.runner import check_host

log = logging.getLogger("judgebox.worker")

VISIBILITY_MS = int(os.environ.get("VISIBILITY_MS", "120000"))
MAX_DELIVERIES = 3
MAX_ATTEMPTS = 3
SWEEP_EVERY_S = 30
LOST_AFTER_S = 60


class Worker:
    def __init__(self, redis_client, sessions, problems_dir, name: str | None = None):
        self.r = redis_client
        self.sessions = sessions
        self.problems_dir = problems_dir
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self._last_sweep = 0.0

    # ------------------------------------------------------------ main loop

    def run(self, stop: threading.Event) -> None:
        jobs.ensure_group(self.r)
        log.info("worker %s ready", self.name)
        while not stop.is_set():
            self.step(block_ms=2000)

    def step(self, block_ms: int = 0) -> int:
        """One round: recover stuck work, then take at most one new message."""
        handled = self.reclaim_stale()
        if time.monotonic() - self._last_sweep > SWEEP_EVERY_S:
            self.requeue_lost()
            self._last_sweep = time.monotonic()
        resp = self.r.xreadgroup(jobs.GROUP, self.name, {jobs.STREAM: ">"}, count=1, block=block_ms or None)
        for _, messages in resp or []:
            for msg_id, fields in messages:
                self.handle(msg_id, int(fields["id"]))
                handled += 1
        return handled

    # ------------------------------------------------------------ one submission

    def handle(self, msg_id: str, submission_id: int) -> None:
        with self.sessions() as s:
            row = s.execute(text("""
                UPDATE submissions SET status = 'running', attempts = attempts + 1
                WHERE id = :id AND status <> 'done'
                RETURNING problem_slug, language, source, attempts
            """), {"id": submission_id}).first()
            s.commit()
        if row is None:
            # Already judged (a duplicate delivery) or deleted.
            self.r.xack(jobs.STREAM, jobs.GROUP, msg_id)
            return

        slug, language, source, attempts = row
        log.info("judging %s (%s, %s), attempt %s", submission_id, slug, language, attempts)

        def emit(event: dict) -> None:
            jobs.publish(self.r, submission_id, event)
            # Reset the message's idle time so a long judge is not mistaken
            # for a dead worker and claimed by someone else.
            self.r.xclaim(jobs.STREAM, jobs.GROUP, self.name, min_idle_time=0,
                          message_ids=[msg_id], justid=True)

        try:
            prob = problems.load(self.problems_dir / slug)
            outcome = judge(prob, language, source, emit)
        except Exception as exc:  # noqa: BLE001 - a judge crash must not kill the worker
            log.exception("judge failed for %s", submission_id)
            outcome = Outcome("SE", message=f"{type(exc).__name__}: {exc}")

        if outcome.verdict == "SE" and attempts < MAX_ATTEMPTS:
            with self.sessions() as s:
                s.execute(text("UPDATE submissions SET status = 'queued' WHERE id = :id AND status <> 'done'"),
                          {"id": submission_id})
                s.commit()
            jobs.publish(self.r, submission_id, {"type": "retrying", "reason": outcome.message[:200]})
            jobs.enqueue(self.r, submission_id)
            self.r.xack(jobs.STREAM, jobs.GROUP, msg_id)
            return

        self.finish(submission_id, outcome)
        self.r.xack(jobs.STREAM, jobs.GROUP, msg_id)

    def finish(self, submission_id: int, outcome: Outcome) -> bool:
        """Store the verdict. Returns False if another worker already did."""
        with self.sessions() as s:
            sub = s.get(db.Submission, submission_id, with_for_update=True)
            if sub is None or sub.status == "done":
                s.rollback()
                return False
            sub.status = "done"
            sub.verdict = outcome.verdict
            sub.cpu_ms = outcome.cpu_ms
            sub.memory_kb = outcome.memory_kb
            sub.message = outcome.message[:2000] or None
            sub.compile_message = outcome.compile_message[:8000] or None
            sub.tests = [asdict(t) for t in outcome.tests]
            sub.judged_at = db.utcnow()
            s.commit()
        jobs.publish(self.r, submission_id, {
            "type": "done",
            "verdict": outcome.verdict,
            "cpu_ms": outcome.cpu_ms,
            "memory_kb": outcome.memory_kb,
            "message": outcome.message,
        })
        log.info("submission %s: %s", submission_id, outcome.verdict)
        return True

    # ------------------------------------------------------------ recovery

    def reclaim_stale(self) -> int:
        """Take over messages that another worker read but never acknowledged."""
        _, claimed, _ = self.r.xautoclaim(jobs.STREAM, jobs.GROUP, self.name,
                                          min_idle_time=VISIBILITY_MS, start_id="0-0", count=5)
        for msg_id, fields in claimed:
            if not fields:  # trimmed from the stream while pending
                self.r.xack(jobs.STREAM, jobs.GROUP, msg_id)
                continue
            submission_id = int(fields["id"])
            pending = self.r.xpending_range(jobs.STREAM, jobs.GROUP, min=msg_id, max=msg_id, count=1)
            deliveries = pending[0]["times_delivered"] if pending else 1
            if deliveries > MAX_DELIVERIES:
                log.error("submission %s failed on %s deliveries, dead-lettering", submission_id, deliveries)
                self.r.xadd(jobs.DEAD, {"id": str(submission_id), "deliveries": str(deliveries)})
                self.finish(submission_id, Outcome("SE", message="judge crashed repeatedly on this submission"))
                self.r.xack(jobs.STREAM, jobs.GROUP, msg_id)
                continue
            log.warning("reclaimed submission %s (delivery %s)", submission_id, deliveries)
            self.handle(msg_id, submission_id)
        return len(claimed)

    def requeue_lost(self) -> int:
        """Re-enqueue rows stuck in 'queued' while the stream has nothing for them.

        That only happens when the API committed the row and then failed to
        reach Redis. Checking that the stream is idle first avoids adding
        duplicates when the queue is simply busy.
        """
        depth = jobs.depth(self.r)
        if depth["waiting"] or depth["in_progress"]:
            return 0
        with self.sessions() as s:
            ids = s.execute(text("""
                SELECT id FROM submissions
                WHERE status = 'queued' AND created_at < now() - make_interval(secs => :age)
                ORDER BY id LIMIT 100
            """), {"age": LOST_AFTER_S}).scalars().all()
        for submission_id in ids:
            log.warning("re-enqueueing lost submission %s", submission_id)
            jobs.enqueue(self.r, submission_id)
        return len(ids)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    check_host()
    cfg = settings.get()
    db.create_schema()
    worker = Worker(jobs.client(cfg.redis_url), db.session_factory(), cfg.problems_dir)

    stop = threading.Event()
    # Finish the current submission on SIGTERM instead of abandoning it.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker.run(stop)
    log.info("worker %s stopped", worker.name)


if __name__ == "__main__":
    main()
