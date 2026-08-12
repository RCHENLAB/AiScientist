"""``bioagent-admin`` — manage accounts from the server shell WITHOUT putting any
password in a file. Passwords are read with ``getpass`` (hidden, in-memory only) and
hashed before they touch the database; nothing plaintext is ever written to disk.

    bioagent-admin create-admin [username]     # create or promote an admin (prompts pw)
    bioagent-admin reset-password <username>   # reset any user's password (prompts pw)
    bioagent-admin list-users                  # list accounts (no hashes)
    bioagent-admin hash-password               # print a bcrypt hash for BIOAGENT_ADMIN_PASSWORD_HASH

Run after the `auth` extra is installed and BIOAGENT_DATABASE_URL points at the DB.
"""

from __future__ import annotations

import argparse
import getpass
import sys


def _prompt_password() -> str:
    pw = getpass.getpass("New password: ")
    if len(pw) < 6:
        print("password must be at least 6 characters", file=sys.stderr)
        raise SystemExit(2)
    if pw != getpass.getpass("Confirm password: "):
        print("passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bioagent-admin", description="AiScientist account management")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin", help="create or promote an admin")
    ca.add_argument("username", nargs="?")
    rp = sub.add_parser("reset-password", help="reset a user's password")
    rp.add_argument("username")
    sub.add_parser("list-users", help="list accounts")
    sub.add_parser("hash-password", help="print a bcrypt hash (for BIOAGENT_ADMIN_PASSWORD_HASH)")
    args = parser.parse_args(argv)

    # Load the project .env (BIOAGENT_DATABASE_URL / SECRET_KEY / ...) the same way the
    # console does, so the CLI hits the SAME database as the running app — regardless of
    # the current working directory. Without this it would silently default to SQLite.
    from pathlib import Path

    from ..core.config import load_project_env
    load_project_env(Path(__file__).resolve().parents[3])

    # Imported here so `--help` works even without the auth extra installed.
    from . import auth, auth_routes
    from .db import init_db

    if args.cmd == "hash-password":
        print(auth.hash_password(_prompt_password()))
        return 0

    init_db()  # ensure tables exist (idempotent)

    if args.cmd == "create-admin":
        username = (args.username or input("Admin username: ")).strip()
        if not username:
            print("username required", file=sys.stderr)
            return 2
        name, created = auth_routes.create_admin_account(username, _prompt_password())
        print(f"{'Created' if created else 'Promoted/updated'} admin: {name}")
        return 0

    if args.cmd == "reset-password":
        ok = auth_routes.set_user_password(args.username, _prompt_password())
        print(f"Password reset for {args.username}" if ok else f"User not found: {args.username}")
        return 0 if ok else 1

    if args.cmd == "list-users":
        for u in auth_routes.all_users():
            flag = "admin" if u["role"] == "admin" else "user"
            state = "active" if u["is_active"] else "DISABLED"
            print(f"  [{u['id']:>3}] {u['username']:<20} {flag:<6} {state}")
        return 0
    return 2


if __name__ == "__main__":  # python -m bioagent.gateway.admin_cli ...
    raise SystemExit(main())
