"""Create an account from the command line (handy inside Docker):

    docker-compose exec web python create_user.py --role teacher --email me@school.edu --password 'a long password' --name "Ms Rao"
    docker-compose exec web python create_user.py --role student --reg-no 12345 --password 1234 --name "A Student"
"""
import argparse
import sys

from app import auth
from app.db import SessionLocal, User, init_db


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--role", choices=["teacher", "student"], required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--email", help="teachers: login email")
    p.add_argument("--reg-no", help="students: 5-digit ID (also their login)")
    a = p.parse_args()
    if a.role == "teacher" and (not a.email or len(a.password) < 8):
        sys.exit("Teachers need --email and a password of at least 8 characters")
    if a.role == "student" and not (a.reg_no and a.reg_no.isdigit() and len(a.reg_no) == 5):
        sys.exit("Students need --reg-no with exactly 5 digits")
    init_db()
    db = SessionLocal()
    email = a.email.strip().lower() if a.role == "teacher" else f"{a.reg_no}@student.local"
    if db.query(User).filter((User.email == email) | ((User.reg_no == a.reg_no) if a.reg_no else False)).first():
        sys.exit("That account already exists")
    db.add(User(email=email, name=a.name, role=a.role, password_hash=auth.hash_password(a.password),
                reg_no=a.reg_no if a.role == "student" else None))
    db.commit()
    print(f"Created {a.role} account: {a.email or a.reg_no}")


if __name__ == "__main__":
    main()
