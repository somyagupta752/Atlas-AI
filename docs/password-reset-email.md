# Password-reset email (Task 15.5)

Reset links are delivered by email over SMTP. Nothing else delivers them: no token or link is ever written to a log, in any
environment.

## Configure

| Setting | Meaning |
| --- | --- |
| `SMTP_HOST`, `SMTP_PORT` | The server. Port 587 with `starttls`, 465 with `ssl`. |
| `SMTP_SECURITY` | `starttls` (default), `ssl`, or `none` (only for a local test server). |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Login. The password is the `smtp_password` secret file (`/run/secrets`), never a variable. |
| `SMTP_FROM`, `SMTP_FROM_NAME` | Sender address (your provider usually requires it to be verified) and display name. |
| `PUBLIC_BASE_URL` | Where users open the dashboard, such as `https://atlas.example.com`. Builds the link in the message. |
| `SMTP_CA_FILE` | Optional CA bundle for a relay with a private CA. |
| `AUTH_PASSWORD_RESET_ENABLED` | `false` switches reset off. The endpoint still gives the same answer. |

Transactional providers (Postmark, SendGrid, Amazon SES, Mailgun and others) publish an SMTP interface. Use their host, port and
SMTP credentials. Nothing in Atlas is specific to one provider.

**Start-up refuses to run** in staging and production when reset is on and `SMTP_HOST`, `SMTP_FROM` or an `https://`
`PUBLIC_BASE_URL` is missing, when the server is not local and `SMTP_SECURITY=none`, or when credentials are half set.

## Security properties

- Tokens: 256 bits from `secrets`, stored only as SHA-256, 30 minutes, single use. A new request voids older unused links.
- Redemption is one atomic `UPDATE ... WHERE used_at IS NULL AND expires_at > now()`. Of any number of simultaneous attempts,
  exactly one succeeds (tested with 8 threads on real commits).
- A refused new password does not spend the link. A successful reset voids every other unused link and signs out every session,
  including access tokens already issued.
- The link carries the token in the URL fragment (`#token=`). Browsers send no fragment to a server or in a `Referer`.
- Privacy: `POST /auth/password-reset` always answers `202` with the same body. The email is sent after the response, so neither
  timing nor a mail error shows whether an account exists. It is rate limited per address and per client.
- Transport: TLS 1.2+, certificate and host name always verified, no downgrade when STARTTLS is missing, no credentials over an
  unencrypted connection.
- Failures: reduced to a category (`connect`, `timeout`, `tls`, `auth`, `rejected`, `protocol`, `message`, `not_configured`,
  `unexpected`) and an SMTP code. Provider text, host, credentials and recipient are never logged. The recipient appears only as a
  12-character one-way label. Counter: `atlas_emails_total{template,outcome}`.

## Operate

- A message that never arrives: look for `password_reset_email_failed` in the API log (field `kind`), and watch
  `atlas_emails_total{outcome!="sent"}`. `auth` means bad SMTP credentials, `tls` a certificate or STARTTLS problem, `rejected` the
  provider refused the sender or recipient (check the verified sender), `connect`/`timeout` network or firewall (the `egress` network
  must reach the mail server).
- No retry queue yet: a failed message is not resent; the user requests a new link. A durable outbox is future work.
- The api runs on internal networks plus `egress`; allow outbound TCP to the SMTP port there.

## Tests

- `tests/auth/test_mail.py`: the real SMTP client against an in-process server (plain, STARTTLS, implicit TLS, bad certificate,
  wrong host name, no STARTTLS, auth, refused recipient, timeout, refused connection), header injection, logs, concurrency.
- `tests/auth/test_mail_config.py`: the fail-closed start-up rules.
- Browser tests read mail from `frontend/e2e/support/mail_server.py`, a local capture service started by Playwright. No real
  server, credentials or email are involved.
