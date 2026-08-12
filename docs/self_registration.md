# Self-registration + email verification

Users can create their own account, gated by (1) a **UCI email address** and (2) a
**6-digit code emailed to that address**. Nothing is minted until the code is verified —
the pending signup (bcrypt-hashed password + hashed code) lives in the
`pending_registrations` table until then.

## Flow

1. `POST /api/auth/register/start {username, email, password}` — validates the username
   (3–64 chars), password (≥6), and that the email is under an allowed domain. Emails a
   6-digit code (15-minute expiry) and stores a hashed copy. Returns `{status:"code_sent",
   email, expires_in_minutes, dev_mode}` (+ `dev_code` in dev mode — see below).
2. `POST /api/auth/register/verify {email, code}` — on the correct code (before expiry,
   under 5 attempts) creates the `User` (role `user`, active), signs them in (session
   cookie), and returns `{status:"ok", user}`.
3. `GET /api/auth/config` — the login UI reads `{self_register, email_domains,
   email_mode}` to show/hide the "Create account" path and explain dev mode.

## Is email free? Can it be local + stable?

**Yes.** Sending email is free; you only choose which relay to send *through*. Because the
console runs on a UCI host and every recipient is an `@uci.edu` address, the natural
zero-cost, stable, effectively-local path is the **campus SMTP relay `smtp.uci.edu`**
(intra-domain → highly deliverable). A local Postfix smart-hosting through that relay works
too. No third-party paid service (SendGrid/SES/etc.) is required.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `BIOAGENT_ALLOW_SELF_REGISTER` | `1` | `0`/`false` disables the whole self-registration channel. |
| `BIOAGENT_ALLOWED_EMAIL_DOMAINS` | `uci.edu` | Comma-separated. A base domain also admits its subdomains (so `uci.edu` accepts `ics.uci.edu`, `hs.uci.edu`, …). |
| `BIOAGENT_SMTP_HOST` | *(unset)* | SMTP relay hostname, e.g. `smtp.uci.edu`. **If unset → dev mode** (see below). |
| `BIOAGENT_SMTP_PORT` | `587` | `587` STARTTLS · `465` implicit TLS · `25` local relay. |
| `BIOAGENT_SMTP_USER` / `BIOAGENT_SMTP_PASSWORD` | *(unset)* | Optional auth (omit for an IP-allowed on-campus relay). |
| `BIOAGENT_SMTP_FROM` | `AiScientist <no-reply@uci.edu>` | From/envelope address. |
| `BIOAGENT_SMTP_TLS` | `starttls` | `starttls` · `ssl` · `none`. |

### Dev mode (no SMTP configured)

If `BIOAGENT_SMTP_HOST` is unset, the server does **not** send email — it logs the code to
stdout (→ `journalctl -u bioagent`) and the `register/start` response includes `dev_code`
so the browser flow is fully testable locally. Once a real relay is configured, `dev_code`
is never returned and codes are only emailed.

### Production example (UCI relay)

```
BIOAGENT_SMTP_HOST=smtp.uci.edu
BIOAGENT_SMTP_PORT=587
BIOAGENT_SMTP_FROM=AiScientist <no-reply@uci.edu>
# BIOAGENT_SMTP_USER / _PASSWORD only if the relay requires auth for your host.
```

### Prerequisite: let RCIC allow this host to relay

Before flipping prod out of dev mode, the eyeserver host must be **permitted to relay
through `smtp.uci.edu`** — otherwise the SMTP connection is refused and `register/start`
returns 502. Two ways, either is fine:

- **IP allow-list** (simplest): ask RCIC/OIT to allow SMTP relay from the eyeserver's
  outbound IP. Then no `BIOAGENT_SMTP_USER/_PASSWORD` is needed.
- **Authenticated submission**: use a departmental/service account's credentials in
  `BIOAGENT_SMTP_USER` / `BIOAGENT_SMTP_PASSWORD`.

Quick check from the host once allowed:

```
python3 -c "import smtplib,ssl; s=smtplib.SMTP('smtp.uci.edu',587,timeout=10); \
s.starttls(context=ssl.create_default_context()); print('relay OK'); s.quit()"
```

Until this is arranged, leave `BIOAGENT_SMTP_HOST` unset — dev mode keeps signup working
(the code is logged to the journal and shown in the browser).

## Admin additions

The Admin view now shows each user's **email**, has a **search box** (fuzzy by email or
username; exact by id when the query is all digits — `GET /api/admin/users?q=…`), and a
**Delete** action (`DELETE /api/admin/users/{id}`) that removes the account + all its
history (datasets/runs/chats cascade) and best-effort deletes the user's own files on
disk. Guards: you can't delete your own account or the last remaining admin.
