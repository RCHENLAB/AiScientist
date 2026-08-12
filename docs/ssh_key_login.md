# SSH-key login (skip password + Duo next time)

A returning user can log in to HPC3 with a saved SSH key instead of password + Duo.

## Flow

1. **First login** with password + Duo. Duo defaults to **push** (approve on your phone —
   no 6-digit code). With **"Remember me"** checked (default), the gateway:
   - generates an **Ed25519** keypair,
   - appends the **public** key to your HPC3 `~/.ssh/authorized_keys` (idempotent, over the
     just-authenticated session),
   - stores the **private** key on the gateway.
2. **Next login**: pick auth method **"Saved SSH key"** → choose the saved credential →
   `auth_publickey`, no Duo.

API: `GET /api/ssh-credentials` (list, public metadata only) · `DELETE
/api/ssh-credentials/{id}` (remove). Endpoints: `gateway/ssh_credentials.py`.

## Where the private key lives — and why it's on the server

Private keys are stored **on the gateway host (eyeserver)** at
`<BIOAGENT_STATE_DIR>/ssh_creds/<owner>/<id>.key` (`0600`, one dir per user).

This is a **web app**, not a local desktop app: the browser has no SSH; the eyeserver
gateway (paramiko) connects to HPC3 on your behalf. So a private key has no "user-local"
home here — the gateway is the only place it can live and be used. (Contrast operon, a
local Tauri desktop app, which drives your own OpenSSH and keeps keys in your `~/.ssh/`.)
This is the standard server-side-credential model (JupyterHub, CI/CD, …).

Only the **public** key is ever sent to HPC3. Private keys never leave the gateway.

## Security notes (read before production)

- eyeserver now holds every user's HPC3 login key → a **high-value target**. A breach
  means passwordless HPC3 access for all users. Keep eyeserver access tight; the
  deployment is UCI-internal.
- Keys are **unencrypted by default** (best UX — truly one-click next time). The login
  form has an **optional** "protect the new key with a passphrase" field; if set, the key
  is encrypted at rest and the passphrase is required (and never stored) to use it.
- Mitigations in place: `0600` private-key files, per-owner directories, public-key-only
  deployment, idempotent `authorized_keys` append.
- Set `BIOAGENT_STATE_DIR` to a persistent, access-controlled path in production (same dir
  as the SQLite DB / other gateway state).

## Config

| Var | Meaning |
|-----|---------|
| `BIOAGENT_STATE_DIR` | Base dir for gateway state; keys go under `ssh_creds/<owner>/`. Default `.`. |

Per-request (`/api/connect`): `duo_method` (`push`|`phone`|`passcode`, default `push`),
`create_key` (mint+deploy a reusable key on a password+Duo login, default on),
`credential_id` (use a saved key), `key_passphrase` (encrypt a new key / unlock a saved one).
