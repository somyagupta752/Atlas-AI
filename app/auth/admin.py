"""Operator commands. ``python -m app.auth.admin create-admin EMAIL`` / ``claim SLUG EMAIL``.

Passwords are read from the ``ATLAS_ADMIN_PASSWORD`` environment variable or a prompt, never from arguments.
"""

import getpass
import os
import sys

from sqlalchemy import select

from app.auth.models import Repository, RepositoryMembership, Role, User
from app.auth.service import AuthService, normalize_email
from app.config import get_settings
from app.database import SessionLocal


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("create-admin", "claim"):
        print(__doc__)
        return 2
    settings = get_settings()
    with SessionLocal() as db:
        if argv[0] == "create-admin":
            email = normalize_email(argv[1])
            password = os.environ.get("ATLAS_ADMIN_PASSWORD") or getpass.getpass("Password: ")
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = AuthService(db, settings).register(email, password)
            user.is_admin = True
            db.commit()
            print(f"admin: {user.email}")
        else:
            slug, email = argv[1], normalize_email(argv[2]) if len(argv) > 2 else ""
            owner = db.scalar(select(User).where(User.email == email))
            if owner is None:
                print("no such user")
                return 1
            repository = db.scalar(select(Repository).where(Repository.slug == slug))
            if repository is None:
                repository = Repository(slug=slug, created_by_id=owner.id)
                db.add(repository)
                db.flush()
            member = db.scalar(select(RepositoryMembership).where(
                RepositoryMembership.repository_id == repository.id, RepositoryMembership.user_id == owner.id))
            if member is None:
                db.add(RepositoryMembership(repository_id=repository.id, user_id=owner.id, role=Role.OWNER.value))
            else:
                member.role = Role.OWNER.value
            db.commit()
            print(f"{slug}: owner {owner.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
