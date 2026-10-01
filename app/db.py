"""Database models (users, sessions, exams, questions, generated sheets, results, queued student uploads).

Runs on SQLite by default; set DATABASE_URL to use PostgreSQL (for example Supabase)."""
import datetime as dt
import os
from typing import Optional
from urllib.parse import quote, unquote

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from omr import config

DB_PATH = os.environ.get("OMR_DB", os.path.join(config.DATA_DIR, "platform.db"))


def database_url() -> str:
    """DATABASE_URL (postgres://... or postgresql://...) if set, otherwise the local SQLite file."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DB_PATH}"
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):   # what Supabase / Render hand out -> psycopg 3 driver
        if url.startswith(prefix):
            return "postgresql+psycopg://" + encode_credentials(url[len(prefix):])
    return url


def encode_credentials(rest: str) -> str:
    """'user:pass@host:port/db' -> percent-encode the user and password, so a password containing @ : / # ? still works.
    The host starts after the LAST '@'."""
    if "@" not in rest:
        return rest
    userinfo, hostpart = rest[:rest.rindex("@")], rest[rest.rindex("@") + 1:]
    user, sep, password = userinfo.partition(":")
    return quote(unquote(user), safe="") + (":" + quote(unquote(password), safe="") if sep else "") + "@" + hostpart


URL = database_url()
IS_SQLITE = URL.startswith("sqlite")
if IS_SQLITE:
    engine = create_engine(URL, connect_args={"check_same_thread": False})
else:                                                          # small pool: free Postgres plans allow few connections
    # prepare_threshold=None: Supabase's pooler (pgbouncer, port 6543) does not support server-side prepared statements
    engine = create_engine(URL, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=300,
                           connect_args={"prepare_threshold": None})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))            # "teacher" | "student"
    password_hash: Mapped[str] = mapped_column(String(100))
    reg_no: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)   # students: matches the bubbled registration number


class AuthSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    header: Mapped[dict] = mapped_column(JSON)               # university, course, ... (shown on the sheet)
    num_students: Mapped[int] = mapped_column(Integer)
    shuffle_questions: Mapped[bool] = mapped_column(default=False)
    shuffle_options: Mapped[bool] = mapped_column(default=False)
    marks_released: Mapped[bool] = mapped_column(default=False)      # students (and the QR card) see marks only when True
    status: Mapped[str] = mapped_column(String(20), default="generating")   # generating | ready | failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    questions: Mapped[list["ExamQuestion"]] = relationship(order_by="ExamQuestion.position", cascade="all, delete-orphan")
    sheets: Mapped[list["Sheet"]] = relationship(order_by="Sheet.student_no", cascade="all, delete-orphan")


class ExamQuestion(Base):
    __tablename__ = "exam_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)                  # raw LaTeX
    options: Mapped[list] = mapped_column(JSON)              # 4 raw-LaTeX strings
    correct: Mapped[int] = mapped_column(Integer)            # 0..3


class Sheet(Base):
    """One printed answer sheet: unique paper ID + QR token, own question order and answer key."""
    __tablename__ = "sheets"
    __table_args__ = (UniqueConstraint("paper_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    student_no: Mapped[int] = mapped_column(Integer)
    paper_id: Mapped[str] = mapped_column(String(6))
    qr_token: Mapped[str] = mapped_column(String(32), unique=True)
    key: Mapped[list] = mapped_column(JSON, default=list)          # correct letter per displayed question
    question_order: Mapped[list] = mapped_column(JSON, default=list)
    option_orders: Mapped[list] = mapped_column(JSON, default=list)
    pdf_path: Mapped[str] = mapped_column(String(500), default="")


class Result(Base):
    """A graded scan of one sheet. Students see the results whose registration number equals their own."""
    __tablename__ = "results"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("sheets.id"), unique=True)      # latest scan replaces the earlier one
    registration: Mapped[str] = mapped_column(String(20), index=True)
    answers: Mapped[dict] = mapped_column(JSON)                                      # question number -> letter | BLANK | MULTIPLE | UNCERTAIN
    score: Mapped[float] = mapped_column(Float)
    total: Mapped[int] = mapped_column(Integer)
    needs_review: Mapped[bool] = mapped_column(default=False)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    image_path: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(10), default="teacher")               # "teacher" (official) | "student" (self-check)
    resolved: Mapped[bool] = mapped_column(default=False)                            # teacher has dealt with the review flags
    original_answers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)    # what the scanner read, for answers the teacher corrected
    edited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScanJob(Base):
    """A student's uploaded sheet waiting its turn; a background worker grades the queue one sheet at a time."""
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))                                   # the saved upload (deleted once graded)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)    # queued | working | done | failed
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)              # what the old synchronous upload returned
    error: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[int] = mapped_column(Integer, default=0)                      # HTTP status the error maps to
    attempts: Mapped[int] = mapped_column(Integer, default=0)                        # times a worker started on it
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


def add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create tables; upgrade databases made by older versions; lock down tables on PostgreSQL."""
    if IS_SQLITE:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if "reg_no" not in {c["name"] for c in inspect(conn).get_columns("users")}:
            add_column_if_missing(conn, "users", "reg_no", "VARCHAR(20)")
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_reg_no ON users(reg_no)"))
        add_column_if_missing(conn, "exams", "marks_released", "BOOLEAN DEFAULT FALSE")
        add_column_if_missing(conn, "results", "source", "VARCHAR(10) DEFAULT 'teacher'")
        add_column_if_missing(conn, "results", "resolved", "BOOLEAN DEFAULT FALSE")
        add_column_if_missing(conn, "results", "original_answers", "JSON")
        add_column_if_missing(conn, "results", "edited_at", "TIMESTAMP")
        add_column_if_missing(conn, "scan_jobs", "attempts", "INTEGER DEFAULT 0")
        add_column_if_missing(conn, "scan_jobs", "started_at", "TIMESTAMP")
        if not IS_SQLITE:
            # Supabase exposes the public schema through its web API. With row level security on and no policies, that API
            # (anon/authenticated keys) can read nothing; this app connects as the table owner, which is not affected.
            for table in Base.metadata.tables:
                conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
