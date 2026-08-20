# PITB User Manager

Bulk and targeted management of **PITB dashboard mobile users** for the Rawalpindi
dengue surveillance program. All scripts share one login flow (cookie session +
arithmetic CAPTCHA) and operate against the administrative dashboard at
`https://dashboard-tracking.punjab.gov.pk`.

## Scripts

| Script | Purpose |
| --- | --- |
| `main.py` | Bulk create / activate mobile users from an Excel workbook. |
| `sweep_staff_access.py` | Grant tehsil access to existing staff from an Excel workbook. |
| `reset_mobile_user_password.py` | Reset one user's password by username, preserving all other fields. |

## Setup

```bash
uv sync
```

All scripts read the **administrative** dashboard credentials from environment
variables and never store them in project files:

- `PITB_USERNAME` — your dashboard admin username.
- `PITB_PASSWORD` — your dashboard admin password.

The password-reset script additionally reads the target account from:

- `MOBILE_USER_USERNAME` — the username to reset (or pass `--username`).
- `MOBILE_USER_PASSWORD` — the new password (or it prompts securely without echo).

> Run with `uv run python <script>` from this directory. A past session needed
> `env -u PYTHONPATH` to avoid a Python 3.11 import shadow; that is no longer
> required here because `uv run` uses the project's Python 3.12 environment.

---

## Bulk create / activate users — `main.py`

Reads every row of the workbook's `Activation` sheet (falls back to the only
sheet when `Activation` is absent). Uses one `requests.Session` to sign in, fetch
a fresh CSRF token per user form, and submit records sequentially.

- Creates a user when no matching account exists.
- Otherwise activates the single matched account through the guarded patch flow.
- Sets status to **Active** and applies the source-data mapping (passwords and
  town-derived tehsils).
- Repeated source CNICs are skipped after their first attempted row.
- When a create reports duplicate CNIC **and** username, it looks up the account
  by username and patches the edit page only when exactly one edit link is found.

Run:

```bash
PITB_USERNAME="your-username" PITB_PASSWORD="your-password" \
uv run python main.py
```

Each row prints one JSON result line, followed by a JSON summary. Failures are
reported per row and do not stop the rest of the batch.

---

## Grant staff access — `sweep_staff_access.py`

Reads the `Sheet2` sheet of `Sweep Staff access.xlsx`. For each `CNIC` + `Access
Required for` row, it looks up the user, then adds the tehsils mapped to that
access type to the account's existing tehsil assignments:

- `cantt` → tehsil IDs `149`, `148`
- `potohar town` → tehsil IDs `145`, `146`

Patches only when exactly one edit link is returned for the CNIC.

Run:

```bash
PITB_USERNAME="your-username" PITB_PASSWORD="your-password" \
uv run python sweep_staff_access.py
```

---

## Reset one user's password — `reset_mobile_user_password.py`

Resets the password for a single mobile user identified by username, while
leaving every other field untouched.

Flow:

1. Signs in with the admin credentials.
2. Fetches the live `/mobile_users` listing and uses its **current** search form
   (the username filter field name is read from the page, not hard-coded).
3. Refuses to proceed unless the search returns **exactly one** edit record.
4. Fetches that user's edit form and verifies its `mobile_user[username]` matches
   the requested username.
5. Submits a PATCH that sends **only** `mobile_user[password]` (the edit form
   does not render password inputs, so no confirmation field is sent).

### Dry run (recommended first)

Authenticates, searches, and verifies the exact-match and form, then prints the
result without submitting any change:

```bash
PITB_USERNAME="your-username" PITB_PASSWORD="your-password" \
MOBILE_USER_USERNAME="3740565599766" \
MOBILE_USER_PASSWORD="new-password" \
uv run python reset_mobile_user_password.py --dry-run
```

Successful dry-run output:

```json
{"matching_records": 1, "username_filter_field": "username", "form_username_matches_search": true, "operation": "dry_run_verified"}
```

### Apply the reset

Remove `--dry-run` to submit the password update:

```bash
PITB_USERNAME="your-username" PITB_PASSWORD="your-password" \
MOBILE_USER_USERNAME="3740565599766" \
MOBILE_USER_PASSWORD="new-password" \
uv run python reset_mobile_user_password.py
```

Successful output:

```json
{"matching_records": 1, "username_filter_field": "username", "form_username_matches_search": true, "operation": "password_updated", "patch_status": 200, "patch_redirects": [302], "messages": []}
```

### Secure prompt alternative

Omit `MOBILE_USER_PASSWORD` to be prompted without the value echoing to the
terminal:

```bash
PITB_USERNAME="your-username" PITB_PASSWORD="your-password" \
uv run python reset_mobile_user_password.py --username 3740565599766
```

### Safety notes

- The script refuses to update when 0, or more than 1, account matches the
  username (it will not touch ambiguous or missing records).
- A non-empty new password is required.
- After any run, rotate the admin password and the new user password, since they
  may have appeared in shell history or terminal output.
