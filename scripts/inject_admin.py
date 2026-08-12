#!/usr/bin/env python3
"""inject_admin.py — re-seed a BioAdmin account directly in the gateway database.

Use this when `bioagent-admin create-admin` is unavailable or the CLI env isn't
loading correctly on the server.  Needs only bcrypt + sqlalchemy (both in the
server venv at /data/BioAgent/env).

USAGE (run on the server as the bioagent user or with sudo):

  sudo -u bioagent /data/BioAgent/env/bin/python3 /tmp/inject_admin.py
  sudo -u bioagent /data/BioAgent/env/bin/python3 /tmp/inject_admin.py BioAdmin
  sudo -u bioagent /data/BioAgent/env/bin/python3 /tmp/inject_admin.py BioAdmin --db /data/BioAgent/bioagent.db

  # Non-interactive (password via env, useful for automation):
  BIOAGENT_ADMIN_PASSWORD=<pw> ... python3 /tmp/inject_admin.py BioAdmin

Username defaults to 'BioAdmin'. The script creates the user if it doesn't exist,
or resets the password + ensures role=admin + is_active=1 if it does.

DB resolution order: --db override > $BIOAGENT_DATABASE_URL > BIOAGENT_DATABASE_URL
parsed from the app's .env (/data/BioAgent/app/.env) > a SQLite fallback guess. The
.env step is what makes a bare `sudo -u bioagent ... inject_admin.py` hit the SAME
Postgres the console uses, instead of silently writing to a stray SQLite file.

PREFERRED ALTERNATIVE — if the app CLI is installed and BIOAGENT_DATABASE_URL is
set in the server .env, use the canonical tool instead:

  sudo -u bioagent /data/BioAgent/env/bin/bioagent-admin create-admin BioAdmin
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


SERVER_DB_CANDIDATES = [
    "/data/BioAgent/bioagent.db",
    "/data/BioAgent/app/bioagent.db",
    "/data/BioAgent/state/bioagent.db",
    "./bioagent.db",
]

# Where the app's .env lives, so this script hits the SAME DB as the running console
# even when sudo strips BIOAGENT_DATABASE_URL out of the environment. This is THE fix
# for "I reset the password but still can't log in": without it, a bare
# `sudo -u bioagent python3 inject_admin.py` silently fell back to a stray SQLite file
# while the app was actually on Postgres.
ENV_CANDIDATES = [
    "/data/BioAgent/app/.env",
    "/data/BioAgent/.env",
    "./.env",
]


def _database_url_from_env_file() -> str:
    """Parse BIOAGENT_DATABASE_URL out of the app's .env (no extra deps), or ''."""
    for path in ENV_CANDIDATES:
        p = Path(path)
        if not p.exists():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, val = line.partition("=")
                if sep and key.strip() == "BIOAGENT_DATABASE_URL":
                    # Strip surrounding quotes and any trailing inline comment.
                    val = val.strip().strip('"').strip("'")
                    if val:
                        print(f"(loaded BIOAGENT_DATABASE_URL from {p})")
                        return val
        except OSError:
            continue
    return ""


def resolve_db_url(override: str | None) -> str:
    if override:
        # Bare path → sqlite URL
        if not override.startswith(("sqlite", "postgresql", "postgres")):
            return f"sqlite:///{Path(override).resolve()}"
        return override

    env_url = os.environ.get("BIOAGENT_DATABASE_URL", "")
    if env_url:
        return env_url

    # Before falling back to a SQLite guess, read the app's .env so we land on the
    # SAME database the console uses (Postgres in prod). This mirrors how the canonical
    # `bioagent-admin` CLI loads the project .env.
    file_url = _database_url_from_env_file()
    if file_url:
        return file_url

    state = os.environ.get("BIOAGENT_STATE_DIR", "")
    if state:
        p = Path(state) / "bioagent.db"
        if p.exists():
            return f"sqlite:///{p.resolve()}"

    for path in SERVER_DB_CANDIDATES:
        if Path(path).exists():
            return f"sqlite:///{Path(path).resolve()}"

    print(
        "ERROR: cannot locate a database.\n"
        "  No BIOAGENT_DATABASE_URL in the environment or any .env, and no bioagent.db found.\n"
        "  Set BIOAGENT_DATABASE_URL=postgresql+psycopg://bioagent:<pw>@127.0.0.1/bioagent\n"
        "  (or sqlite:////data/BioAgent/bioagent.db), or pass --db /path/or/url.",
        file=sys.stderr,
    )
    sys.exit(1)


def get_password() -> str:
    pw = os.environ.get("BIOAGENT_ADMIN_PASSWORD", "")
    if pw:
        if len(pw) < 6:
            print("ERROR: BIOAGENT_ADMIN_PASSWORD must be >= 6 characters.", file=sys.stderr)
            sys.exit(2)
        return pw

    pw = getpass.getpass("New password: ")
    if len(pw) < 6:
        print("ERROR: password must be at least 6 characters.", file=sys.stderr)
        sys.exit(2)
    if pw != getpass.getpass("Confirm password: "):
        print("ERROR: passwords do not match.", file=sys.stderr)
        sys.exit(2)
    return pw


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-seed a BioAdmin account in the gateway DB")
    ap.add_argument("username", nargs="?", default="BioAdmin", help="admin username (default: BioAdmin)")
    ap.add_argument("--db", metavar="PATH_OR_URL", help="DB path or SQLAlchemy URL (overrides env)")
    args = ap.parse_args()

    db_url = resolve_db_url(args.db)
    username = args.username.strip()
    print(f"DB  : {db_url}")
    print(f"User: {username}")

    try:
        import bcrypt
    except ImportError:
        sys.exit("ERROR: bcrypt not installed. Run: pip install bcrypt")

    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        sys.exit("ERROR: sqlalchemy not installed. Run: pip install sqlalchemy")

    password = get_password()
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, future=True, connect_args=connect_args)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, role, is_active FROM users WHERE username = :u"),
            {"u": username},
        ).fetchone()

        if row is not None:
            conn.execute(
                text(
                    "UPDATE users "
                    "SET password_hash = :h, role = :role, is_active = :active "
                    "WHERE username = :u"
                ),
                # Pass a Python bool (NOT 1) so it works on Postgres boolean columns too.
                {"h": pw_hash, "u": username, "role": "admin", "active": True},
            )
            conn.commit()
            print(f"✓ Updated '{username}' (id={row[0]}): role=admin  is_active=true  password reset.")
        else:
            # Table must already exist (the app creates it on first start). Insert with a
            # Python bool for is_active so the same SQL works on SQLite and Postgres.
            conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, role, is_active) "
                    "VALUES (:u, :h, :role, :active)"
                ),
                {"u": username, "h": pw_hash, "role": "admin", "active": True},
            )
            conn.commit()
            new_id = conn.execute(
                text("SELECT id FROM users WHERE username = :u"), {"u": username}
            ).fetchone()[0]
            print(f"✓ Created '{username}' (id={new_id}): role=admin  is_active=1.")

    print("\nDone. Try logging in now.")


if __name__ == "__main__":
    main()
