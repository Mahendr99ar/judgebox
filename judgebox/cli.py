"""Admin commands.

    python -m judgebox.cli create-admin <username>     (password from stdin or JUDGEBOX_ADMIN_PASSWORD)
    python -m judgebox.cli sync-problems
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import select

from judgebox import db
from judgebox.api.auth import hash_password


def create_admin(username: str) -> None:
    password = os.environ.get("JUDGEBOX_ADMIN_PASSWORD") or getpass.getpass("password: ")
    if len(password) < 12:
        sys.exit("admin password must be at least 12 characters")
    db.create_schema()
    with db.session_factory()() as s:
        user = s.scalar(select(db.User).where(db.User.username == username))
        if user is None:
            user = db.User(username=username)
            s.add(user)
        user.password_hash = hash_password(password)
        user.role = "admin"
        s.commit()
    print(f"{username} is an admin")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="judgebox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("username")
    sub.add_parser("sync-problems")
    args = ap.parse_args(argv)

    if args.cmd == "create-admin":
        create_admin(args.username)
    else:
        from judgebox.api.app import sync_problems

        db.create_schema()
        print(f"{sync_problems()} problems synced")


if __name__ == "__main__":
    main()
