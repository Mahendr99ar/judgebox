from __future__ import annotations

from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status

from judgebox import db, settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def issue_token(user: db.User) -> str:
    cfg = settings.get()
    now = db.utcnow()
    # The role is not put in the token: it is read from the database on every
    # request, so demoting an admin takes effect immediately.
    claims = {"sub": str(user.id), "iat": now, "exp": now + timedelta(hours=cfg.token_hours)}
    return jwt.encode(claims, cfg.jwt_secret, algorithm="HS256")


def _token_from(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    # EventSource cannot set headers, so the live-verdict stream passes it here.
    return request.query_params.get("token")


def current_user(request: Request) -> db.User:
    token = _token_from(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    try:
        claims = jwt.decode(token, settings.get().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from None
    with db.session_factory()() as s:
        user = s.get(db.User, int(claims["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account no longer exists")
    return user


def admin_user(user: db.User = Depends(current_user)) -> db.User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")
    return user
