"""Diagnose the DATABASE_URL connection step by step (the password is never printed).

    python check_db.py            # reads DATABASE_URL from the environment or .env
Run it on the machine / network where the app will run (your laptop, a phone hotspot, the Render shell...).
"""
import os
import re
import socket
import struct
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_env()
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL is not set (environment or .env)")
    from sqlalchemy import text
    from app import db
    if db.IS_SQLITE:
        sys.exit("DATABASE_URL is empty - the app is using SQLite")
    host_port = re.sub(r".*@", "", db.URL).split("/")[0]
    host, _, port = host_port.partition(":")
    port = int(port or 5432)
    print(f"1. target: {host}:{port}")
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        print(f"2. DNS ok: {', '.join(addrs[:3])}")
    except OSError as e:
        sys.exit(f"2. DNS FAILED: {e}  -> check the host in DATABASE_URL (a stray '@' or typo?)")
    try:
        s = socket.create_connection((addrs[0], port), timeout=8)
        s.sendall(struct.pack("!II", 8, 80877103))                  # Postgres SSLRequest
        reply = s.recv(1)
        s.close()
        print(f"3. server answered the Postgres handshake: {reply!r}")
    except OSError as e:
        sys.exit(f"3. NETWORK BLOCKED: {type(e).__name__} - this network/firewall drops Postgres traffic to {host}:{port}.\n"
                 f"   Try another network (phone hotspot) or run this on the server that will host the app.")
    try:
        with db.engine.connect() as c:
            print("4. login ok as", c.execute(text("select current_user")).scalar(), "| PostgreSQL", c.execute(text("show server_version")).scalar())
    except Exception as e:
        sys.exit("4. LOGIN FAILED: " + re.sub(r"password=\S+", "password=****", str(e).splitlines()[0])[:250] +
                 "\n   Check the project ref in the user name, the region in the host, and the password.")
    print("Database connection works.")


if __name__ == "__main__":
    main()
