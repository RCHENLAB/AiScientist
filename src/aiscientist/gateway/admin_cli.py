"""``aiscientist-admin`` — manage accounts from the server shell WITHOUT putting any
password in a file. Passwords are read with ``getpass`` (hidden, in-memory only) and
hashed before they touch the database; nothing plaintext is ever written to disk.

    aiscientist-admin create-admin [username]     # create or promote an admin (prompts pw)
    aiscientist-admin reset-password <username>   # reset any user's password (prompts pw)
    aiscientist-admin list-users                  # list accounts (no hashes)
    aiscientist-admin hash-password               # print a bcrypt hash for AISCIENTIST_ADMIN_PASSWORD_HASH
    aiscientist-admin metadata-check              # draft missing Croissant metadata, re-validate all of it
    aiscientist-admin migrate                     # apply the database migrations this release ships
    aiscientist-admin active-runs                 # how many runs are in progress (0 = safe to restart)

API keys (RCHENLAB/AiScientist issue 16; run as the service account, which owns the key store):

    aiscientist-admin list-llm-keys               # every saved API key: owner, label, hint, paused
    aiscientist-admin pause-llm-key <owner> <id> [--reason TEXT]    # stop one key now
    aiscientist-admin resume-llm-key <owner> <id>
    aiscientist-admin purge-llm-keys [--dry-run] [--also-master-key] [--yes]
                                                  # INCIDENT: delete every saved API key at once
    aiscientist-admin key-service-check           # the key service holds and can read every saved key

Email (a dry run unless --send; one message per account, addressed to that account only):

    aiscientist-admin email-users --subject S --body-file F [--send] [--yes]
    aiscientist-admin email-users --template key-incident [--send] [--yes]

Run after the `auth` extra is installed and AISCIENTIST_DATABASE_URL points at the DB.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path


def _prompt_password() -> str:
    pw = getpass.getpass("New password: ")
    if len(pw) < 6:
        print("password must be at least 6 characters", file=sys.stderr)
        raise SystemExit(2)
    if pw != getpass.getpass("Confirm password: "):
        print("passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return pw


# --- API keys -------------------------------------------------------------------------------------


def _state_dir(args: argparse.Namespace) -> Path | None:
    """The gateway's state dir: ``--state-dir``, else AISCIENTIST_STATE_DIR. Never the current
    directory by default: a key command run from the wrong place must not act on a stray store."""
    raw = (getattr(args, "state_dir", None) or os.environ.get("AISCIENTIST_STATE_DIR", "")).strip()
    if not raw:
        print("AISCIENTIST_STATE_DIR is not set (and no --state-dir): refusing to guess where the "
              "key store is.", file=sys.stderr)
        return None
    os.environ["AISCIENTIST_STATE_DIR"] = raw
    return Path(raw)


def _llm_key_command(args: argparse.Namespace) -> int:
    state = _state_dir(args)
    if state is None:
        return 2
    from . import llm_credentials
    if args.cmd == "key-service-check":
        return _key_service_check()
    if args.cmd == "list-llm-keys":
        rows = llm_credentials.all_credentials()
        for owner, r in rows:
            state_ = (f"PAUSED by {r['paused_by']}" if r.get("paused_at") else "active")
            print(f"  {owner:<20} {r['id']:<13} {str(r.get('label') or '')[:24]:<24} "
                  f"{str(r.get('provider') or ''):<10} key {r.get('key_hint') or '':<7} {state_}")
        print(f"{len(rows)} saved key(s). Session-only keys live in the gateway's memory: see "
              "Admin › API keys in the console.")
        return 0
    if args.cmd in ("pause-llm-key", "resume-llm-key"):
        owner = "".join(c for c in args.owner if c.isalnum() or c in "-_") or "guest"
        paused = args.cmd == "pause-llm-key"
        cred = llm_credentials.set_paused(owner, args.credential_id, paused, by="admin:cli",
                                          reason=getattr(args, "reason", "") or "paused by an administrator")
        if cred is None:
            print(f"No credential {args.credential_id} for {owner}.", file=sys.stderr)
            return 1
        print(f"{'Paused' if paused else 'Resumed'} {owner}'s key {cred['label']} ({cred['key_hint']}). "
              + ("A running gateway refuses it from its next model call." if paused else ""))
        if paused:
            from . import key_guard
            done = key_guard.notify_owner(owner, None, kind="admin", label=cred["label"],
                                          reason=getattr(args, "reason", ""), cred_ids=[cred["id"]])
            print("The owner was emailed." if done.get("emailed") else
                  "The owner was not emailed (no address on the account, or no accounts database).")
        return 0
    # purge-llm-keys
    summary = llm_credentials.store_summary()
    print(f"Key store under {state}: {summary['credentials']} saved key(s) of {summary['owners']} "
          f"account(s), {summary['key_files']} key file(s); master key "
          f"{'present' if summary['master_key'] else 'absent'}.")
    print("This deletes every saved API key of every user. A running gateway then refuses every key "
          "it already read, so runs in progress stop at their next model call, and session-only keys "
          "end too." + (" The master key is deleted as well: every encrypted copy of a key, backups "
                        "included, becomes unreadable." if args.also_master_key else ""))
    if args.dry_run:
        print("Dry run: nothing was removed.")
        return 0
    if not args.yes:
        phrase = "delete all keys"
        if input(f'Type "{phrase}" to go on: ').strip().lower() != phrase:
            print("Not confirmed: nothing was removed.")
            return 1
    done = llm_credentials.purge_all(also_master_key=args.also_master_key)
    left = done.get("left") or {}
    print(f"Removed {done['credentials']} saved key(s) of {done['owners']} account(s); revocation "
          f"marker written.")
    if left.get("key_files") or left.get("owners"):
        print(f"NOT everything was removed ({left}): run this as the service account that owns the "
              "key store.", file=sys.stderr)
        return 1
    print("Next: tell the users to rotate their keys at the provider and add them again "
          "(aiscientist-admin email-users --template key-incident).")
    return 0


def _key_service_check() -> int:
    """Every saved key the web server lists is held by the key service, readable there, and the same
    key (fingerprint). Exit 0 only then. Counts and ids only; no key is read here."""
    from . import key_client, llm_credentials
    if not key_client.enabled():
        print("AISCIENTIST_KEY_SERVICE is not set: this server keeps the keys itself.", file=sys.stderr)
        return 2
    st = key_client.status()
    print(f"Key service at {key_client.service_url()}: {st.get('held')} key(s) held, "
          f"{st.get('readable')} readable, {st.get('session_keys')} session key(s).")
    bad = 0
    rows = [(o, r) for o, r in llm_credentials.all_credentials() if not r.get("session_only")]
    for owner, row in rows:
        got = key_client.get(owner, row["id"])
        if got is None or not got.get("readable") or got.get("key_fingerprint") != row.get("key_fingerprint"):
            bad += 1
            why = "not held" if got is None else "not readable" if not got.get("readable") else "a different key"
            print(f"  {owner} {row['id']} ({row.get('label')}): {why}", file=sys.stderr)
    print(f"{len(rows) - bad} of {len(rows)} saved key(s) held and readable by the key service.")
    return 1 if bad else 0


# --- email to every account -----------------------------------------------------------------------

EMAIL_TEMPLATES = {
    "key-incident": (
        "AiScientist: rotate your API key",
        "We removed every API key saved on AiScientist as a precaution after a security incident "
        "on the server.\n\n"
        "What to do:\n"
        "1. In your provider's console (for Claude: platform.claude.com, API keys), revoke the key "
        "you used with AiScientist and check its recent usage there.\n"
        "2. Create a new key, with a monthly spend limit on its workspace.\n"
        "3. Add the new key under API endpoints in AiScientist (or use it for one session only, "
        "without saving it).\n\n"
        "Runs that were in progress when the keys were removed stopped at their next model call; "
        "their files are kept.\n\n"
        "This message never contains a key, and AiScientist never asks for one by email.\n"),
}


def _email_users(args: argparse.Namespace, users: list[dict]) -> int:
    from . import email_send
    if args.template:
        subject, body = EMAIL_TEMPLATES[args.template]
        subject = args.subject or subject
    else:
        if not args.subject or not args.body_file:
            print("Give --subject and --body-file, or --template.", file=sys.stderr)
            return 2
        subject, body = args.subject, Path(args.body_file).read_text(encoding="utf-8")
    recipients = [u for u in users if u.get("is_active") and (u.get("email") or "").strip()
                  and (not args.role or u.get("role") == args.role)]
    skipped = sum(1 for u in users if u.get("is_active") and not (u.get("email") or "").strip())
    print(f"To: {len(recipients)} active account(s) with an email address "
          f"({skipped} active account(s) have none). Relay: {email_send.email_mode()}.")
    print(f"Subject: {subject}\n\n{body}")
    if not args.send:
        print("Dry run: nothing was sent. Add --send to send.")
        return 0
    if not args.yes:
        phrase = f"send to {len(recipients)}"
        if input(f'Type "{phrase}" to go on: ').strip().lower() != phrase:
            print("Not confirmed: nothing was sent.")
            return 1
    sent = failed = 0
    for i, u in enumerate(recipients):
        if email_send.send_email(u["email"].strip(), subject, body):
            sent += 1
        else:
            failed += 1
            print(f"  not sent to account {u.get('username')}", file=sys.stderr)
        if args.pause > 0 and i + 1 < len(recipients):
            time.sleep(args.pause)
    print(f"Sent {sent}, failed {failed}.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiscientist-admin", description="AiScientist account management")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin", help="create or promote an admin")
    ca.add_argument("username", nargs="?")
    rp = sub.add_parser("reset-password", help="reset a user's password")
    rp.add_argument("username")
    sub.add_parser("list-users", help="list accounts")
    sub.add_parser("hash-password", help="print a bcrypt hash (for AISCIENTIST_ADMIN_PASSWORD_HASH)")
    sub.add_parser("metadata-check", help="draft missing Croissant metadata and re-validate every stored "
                                          "croissant.json now (the gateway also does this weekly)")
    sub.add_parser("migrate", help="apply the database migrations this release ships (every deploy runs it)")
    sub.add_parser("active-runs", help="print how many runs are in progress; a restart interrupts them")
    for name, helptext in (("list-llm-keys", "list every saved API key (no key, no path)"),
                           ("pause-llm-key", "pause one saved API key: no model call uses it"),
                           ("resume-llm-key", "resume an API key an admin or the spike check paused"),
                           ("purge-llm-keys", "INCIDENT: delete every saved API key of every user"),
                           ("key-service-check", "check that the key service (AISCIENTIST_KEY_SERVICE) "
                                                 "holds and can read every saved API key")):
        kp = sub.add_parser(name, help=helptext)
        kp.add_argument("--state-dir", help="the gateway's AISCIENTIST_STATE_DIR (default: from the env)")
        if name in ("pause-llm-key", "resume-llm-key"):
            kp.add_argument("owner")
            kp.add_argument("credential_id")
        if name == "pause-llm-key":
            kp.add_argument("--reason", default="")
        if name == "purge-llm-keys":
            kp.add_argument("--dry-run", action="store_true", help="say what would be removed, remove nothing")
            kp.add_argument("--also-master-key", action="store_true",
                            help="also delete the master key: every encrypted copy of a key (backups "
                                 "included) becomes unreadable")
            kp.add_argument("--yes", action="store_true", help="do not ask for the confirmation phrase")
    em = sub.add_parser("email-users", help="email every active account (a dry run unless --send)")
    em.add_argument("--subject")
    em.add_argument("--body-file", help="a plain-text file with the message")
    em.add_argument("--template", choices=sorted(EMAIL_TEMPLATES), help="a prepared message")
    em.add_argument("--role", choices=("user", "admin"), help="only accounts with this role")
    em.add_argument("--send", action="store_true", help="really send (default: print what would be sent)")
    em.add_argument("--yes", action="store_true", help="do not ask for the confirmation phrase")
    em.add_argument("--pause", type=float, default=0.5, help="seconds between two messages (default 0.5)")
    args = parser.parse_args(argv)

    # Load the project .env (AISCIENTIST_DATABASE_URL / SECRET_KEY / ...) the same way the
    # console does, so the CLI hits the SAME database as the running app — regardless of
    # the current working directory. Without this it would silently default to SQLite.
    from pathlib import Path

    from ..core.config import load_project_env
    load_project_env(Path(__file__).resolve().parents[3])

    if args.cmd in ("list-llm-keys", "pause-llm-key", "resume-llm-key", "purge-llm-keys",
                    "key-service-check"):
        # No database needed: the key store is files, and in an incident the DB may be the problem.
        return _llm_key_command(args)

    # Imported here so `--help` works even without the auth extra installed.
    from . import auth, auth_routes
    from .db import init_db

    if args.cmd == "hash-password":
        print(auth.hash_password(_prompt_password()))
        return 0

    if args.cmd == "migrate":
        from .db import migrate
        before, after = migrate()
        print(f"database schema: {before or 'unversioned'} -> {after}" if before != after
              else f"database schema: {after}, nothing to do")
        return 0

    init_db()  # ensure tables exist (idempotent)

    if args.cmd == "active-runs":
        print(auth_routes.active_run_count())
        return 0

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

    if args.cmd == "metadata-check":
        from . import metadata_store
        from ..metadata import mlcroissant_version
        summary = metadata_store.recheck_all()
        print(f"validator: aiscientist + {('mlcroissant ' + mlcroissant_version()) if mlcroissant_version() else 'no mlcroissant (own rules only)'}")
        print(f"drafted {summary['drafted']}, checked {summary['checked']}, invalid {summary['invalid']}")
        return 1 if summary["invalid"] else 0

    if args.cmd == "email-users":
        return _email_users(args, auth_routes.all_users())

    if args.cmd == "list-users":
        for u in auth_routes.all_users():
            flag = "admin" if u["role"] == "admin" else "user"
            state = "active" if u["is_active"] else "DISABLED"
            print(f"  [{u['id']:>3}] {u['username']:<20} {flag:<6} {state}")
        return 0
    return 2


if __name__ == "__main__":  # python -m aiscientist.gateway.admin_cli ...
    raise SystemExit(main())
