"""Submission queue on Redis Streams, and live events for the browser.

A stream with a consumer group gives at-least-once delivery: a message read
by a worker stays pending until the worker acknowledges it, and a worker that
dies leaves its message pending for another worker to claim. Redis lists
(BRPOP) or plain pub/sub would lose the job with the worker.

At-least-once means a submission can be judged twice. The database update
that stores the verdict only applies once (see worker/main.py), so a duplicate
run is wasted work, never a double-counted result.
"""
from __future__ import annotations

import json

import redis

STREAM = "jb:submissions"
GROUP = "judges"
DEAD = "jb:dead"
MAX_STREAM_LEN = 100_000


def client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True)


def ensure_group(r: redis.Redis) -> None:
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def enqueue(r: redis.Redis, submission_id: int) -> str:
    return r.xadd(STREAM, {"id": str(submission_id)}, maxlen=MAX_STREAM_LEN, approximate=True)


def depth(r: redis.Redis) -> dict[str, int]:
    """Messages not yet delivered, and delivered but not yet acknowledged."""
    ensure_group(r)
    info = next((g for g in r.xinfo_groups(STREAM) if g["name"] == GROUP), {})
    return {"waiting": info.get("lag") or 0, "in_progress": info.get("pending", 0)}


# ---------------------------------------------------------------- live events
# Each event goes to a short-lived list (so a page that connects late can
# replay what it missed) and to a pub/sub channel (so a connected page gets it
# immediately). `seq` lets the reader drop the overlap between the two.

def log_key(submission_id: int) -> str:
    return f"jb:events:{submission_id}"


def channel(submission_id: int) -> str:
    return f"jb:live:{submission_id}"


def publish(r: redis.Redis, submission_id: int, event: dict) -> None:
    key = log_key(submission_id)
    seq = r.rpush(key, "")  # reserve a slot; its index is the sequence number
    event = {**event, "seq": seq}
    payload = json.dumps(event)
    pipe = r.pipeline()
    pipe.lset(key, seq - 1, payload)
    pipe.expire(key, 3600)
    pipe.publish(channel(submission_id), payload)
    pipe.execute()


def replay(r: redis.Redis, submission_id: int) -> list[dict]:
    return [json.loads(x) for x in r.lrange(log_key(submission_id), 0, -1) if x]


def subscribe(r: redis.Redis, submission_id: int):
    p = r.pubsub(ignore_subscribe_messages=True)
    p.subscribe(channel(submission_id))
    return p
