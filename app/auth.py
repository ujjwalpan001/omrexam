"""Password hashing, cookie sessions, role checks."""
import hashlib
import secrets

import bcrypt
from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from .db import AuthSession, SessionLocal, User

COOKIE = "omr_session"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=_digest(token), user_id=user.id))
    db.commit()
    return token


def drop_session(db: Session, token: str) -> None:
    db.query(AuthSession).filter_by(token_hash=_digest(token)).delete()
    db.commit()


def current_user(omr_session: str | None = Cookie(default=None), db: Session = Depends(get_db)) -> User:
    if not omr_session:
        raise HTTPException(401, "Not logged in")
    row = db.get(AuthSession, _digest(omr_session))
    user = db.get(User, row.user_id) if row else None
    if user is None:
        raise HTTPException(401, "Session expired")
    return user


def teacher_only(user: User = Depends(current_user)) -> User:
    if user.role != "teacher":
        raise HTTPException(403, "Teachers only")
    return user


def student_only(user: User = Depends(current_user)) -> User:
    if user.role != "student":
        raise HTTPException(403, "Students only")
    return user
