import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBLEMS = ROOT / "problems"

# Settings are read once per process, so the test environment has to be in
# place before anything imports judgebox.settings.
os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 32)
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres@/judgebox_test?host=/tmp")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("PROBLEMS_DIR", str(PROBLEMS))
os.environ.setdefault("SUBMIT_PER_MINUTE", "1000")

needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root for namespaces and cgroups")


@pytest.fixture(autouse=True, scope="session")
def _checker_cache(tmp_path_factory):
    os.environ.setdefault("JUDGEBOX_CACHE", str(tmp_path_factory.mktemp("checkers")))


@pytest.fixture
def services():
    """A clean database and Redis for one test. Skips when they are not running."""
    import redis
    from sqlalchemy.exc import OperationalError

    from judgebox import db, jobs, settings

    try:
        db.Base.metadata.drop_all(db.engine())
        db.Base.metadata.create_all(db.engine())
        r = jobs.client(settings.get().redis_url)
        r.flushdb()
    except (OperationalError, redis.ConnectionError):
        pytest.skip("Postgres or Redis not running")
    jobs.ensure_group(r)
    yield db.session_factory(), r
    r.flushdb()
