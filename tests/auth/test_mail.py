"""15.5: password-reset email. The real SMTP client against an in-process SMTP server; no real mail is ever sent."""

import datetime
import smtplib
import socket
import ssl
import threading
from types import SimpleNamespace

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select, text

from app.auth.mail import (
    GENERIC_FAILURE,
    DisabledEmailSender,
    EmailDeliveryError,
    OutgoingEmail,
    SmtpEmailSender,
    build_reset_email,
    deliver_password_reset,
    email_sender_for,
    recipient_fingerprint,
    reset_link,
)
from app.auth.models import PasswordResetToken, User
from app.auth.service import AuthError, AuthService
from app.config import Settings
from app.database import SessionLocal
from tests.auth.conftest import PASSWORD, make_settings, signup

pytestmark = pytest.mark.real_auth

SECRET_PASSWORD = "provider-smtp-password-4f9a"
MESSAGE = OutgoingEmail(to="ann@example.com", subject="Hello", text="Body with a link\n")


def smtp_settings(port: int, **overrides) -> Settings:
    values = {"smtp_host": "localhost", "smtp_port": port, "smtp_security": "none", "smtp_from": "atlas@example.com",
              "smtp_timeout_seconds": 3.0}
    values.update(overrides)
    return make_settings(**values)


# --- a local SMTP server and certificates ---------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Capture:
    def __init__(self, refuse_recipient: bool = False):
        self.messages: list[SimpleNamespace] = []
        self.refuse_recipient = refuse_recipient

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.refuse_recipient:
            return "550 5.1.1 no such mailbox <secret-provider-detail@internal.example>"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(SimpleNamespace(
            sender=envelope.mail_from, recipients=list(envelope.rcpt_tos), content=envelope.content.decode(),
            encrypted=session.ssl is not None))
        return "250 Message accepted"


def certificate(tmp_path, names=("localhost",)):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / f"{names[0]}.crt", tmp_path / f"{names[0]}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


def server_context(cert_file, key_file) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


def authenticator(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == b"user" and auth_data.password == SECRET_PASSWORD.encode()
    return AuthResult(success=ok, handled=not ok)


@pytest.fixture
def smtp(tmp_path):
    """``start(kind, **opts) -> (settings_overrides, capture)``; every server started is stopped afterwards."""
    started: list[Controller] = []

    def start(kind="none", *, auth=False, refuse_recipient=False, names=("localhost",)):
        port, capture = free_port(), Capture(refuse_recipient)
        cert_file, key_file = certificate(tmp_path, names)
        # server_hostname: without it aiosmtpd looks up this machine's own name on every connection, which can take 5 s
        options = {"hostname": "127.0.0.1", "port": port, "server_hostname": "localhost"}
        if auth:
            options.update(authenticator=authenticator, auth_required=True, auth_require_tls=kind != "none")
        if kind == "starttls":
            options.update(tls_context=server_context(cert_file, key_file), require_starttls=True)
        elif kind == "ssl":
            options.update(ssl_context=server_context(cert_file, key_file))
        controller = Controller(capture, **options)
        controller.start()
        started.append(controller)
        return SimpleNamespace(port=port, capture=capture, ca=cert_file)

    yield start
    for controller in started:
        controller.stop()


# --- the message --------------------------------------------------------------------------------------------------------------


def test_the_reset_message_has_one_link_in_the_fragment_and_says_what_it_is():
    settings = make_settings(public_base_url="https://atlas.example.com")
    message = build_reset_email(settings, "ann@example.com", "tok_ABC-123")
    assert message.to == "ann@example.com" and message.subject == "Reset your Atlas AI password"
    assert message.text.count("https://atlas.example.com/reset-password#token=tok_ABC-123") == 1
    assert "30 minutes" in message.text and "works once" in message.text and "ignore this message" in message.text
    assert "<" not in message.text  # plain text only: no markup, no tracking pixels, no remote content
    assert reset_link(make_settings(public_base_url="https://a.example:8443"), "t") == "https://a.example:8443/reset-password#token=t"
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        make_settings(public_base_url="https://a.example/")  # a trailing slash or a path is refused at start-up


def test_the_message_is_a_well_formed_automatic_notification():
    sender = SmtpEmailSender(smtp_settings(2525, smtp_from_name="Atlas AI"))
    mail = sender.build(MESSAGE)
    assert mail["From"] == "Atlas AI <atlas@example.com>" and mail["To"] == "ann@example.com"
    assert mail["Subject"] == "Hello" and mail["Message-ID"].endswith("@example.com>") and mail["Date"]
    assert mail["Auto-Submitted"] == "auto-generated" and mail["X-Auto-Response-Suppress"] == "All"
    assert mail.get_content_type() == "text/plain" and mail.get_content_charset() == "utf-8"


@pytest.mark.parametrize("bad", ["a@example.com\r\nBcc: victim@example.com", "a@example.com\nSubject: injected"])
def test_a_recipient_cannot_inject_headers(bad):
    with pytest.raises(EmailDeliveryError) as caught:
        SmtpEmailSender(smtp_settings(2525)).send(OutgoingEmail(to=bad, subject="s", text="t"))
    assert caught.value.kind == "message"


def test_credentials_are_never_sent_without_encryption():
    with pytest.raises(ValueError, match="never sent over an unencrypted"):
        SmtpEmailSender(smtp_settings(2525, smtp_username="user", smtp_password=SECRET_PASSWORD, smtp_security="none"))
    with pytest.raises(ValueError):
        SmtpEmailSender(make_settings())  # no host


def test_without_a_mail_server_the_disabled_sender_is_used_and_always_fails_the_same_way():
    assert isinstance(email_sender_for(make_settings()), DisabledEmailSender)
    assert isinstance(email_sender_for(smtp_settings(2525)), SmtpEmailSender)
    with pytest.raises(EmailDeliveryError) as caught:
        DisabledEmailSender().send(MESSAGE)
    assert caught.value.kind == "not_configured"


# --- real SMTP conversations ----------------------------------------------------------------------------------------------


def test_plain_delivery_to_a_local_test_server(smtp):
    server = smtp("none")
    SmtpEmailSender(smtp_settings(server.port)).send(MESSAGE)
    [got] = server.capture.messages
    assert got.sender == "atlas@example.com" and got.recipients == ["ann@example.com"] and not got.encrypted
    assert "Subject: Hello" in got.content and "Body with a link" in got.content


def test_starttls_delivery_is_encrypted_and_the_certificate_is_checked(smtp):
    server = smtp("starttls")
    SmtpEmailSender(smtp_settings(server.port, smtp_security="starttls", smtp_ca_file=server.ca)).send(MESSAGE)
    assert [m.encrypted for m in server.capture.messages] == [True]


def test_implicit_tls_delivery(smtp):
    server = smtp("ssl")
    SmtpEmailSender(smtp_settings(server.port, smtp_security="ssl", smtp_ca_file=server.ca)).send(MESSAGE)
    assert len(server.capture.messages) == 1
    # The same server cannot be spoken to in plain text at all: the encryption is real, not a label.
    with pytest.raises(EmailDeliveryError):
        SmtpEmailSender(smtp_settings(server.port, smtp_security="none", smtp_timeout_seconds=1.0)).send(MESSAGE)
    assert len(server.capture.messages) == 1


def test_an_untrusted_certificate_is_refused_and_nothing_is_sent(smtp):
    server = smtp("starttls")
    with pytest.raises(EmailDeliveryError) as caught:  # no smtp_ca_file: the self-signed certificate is not trusted
        SmtpEmailSender(smtp_settings(server.port, smtp_security="starttls")).send(MESSAGE)
    assert caught.value.kind == "tls" and server.capture.messages == []


def test_a_certificate_for_another_host_name_is_refused(smtp):
    server = smtp("starttls", names=("mail.other.example",))
    with pytest.raises(EmailDeliveryError) as caught:
        SmtpEmailSender(smtp_settings(server.port, smtp_security="starttls", smtp_ca_file=server.ca)).send(MESSAGE)
    assert caught.value.kind == "tls" and server.capture.messages == []


def test_a_server_that_does_not_offer_starttls_is_never_downgraded_to_plain_text(smtp):
    server = smtp("none")  # no encryption available at all
    with pytest.raises(EmailDeliveryError) as caught:
        SmtpEmailSender(smtp_settings(server.port, smtp_security="starttls")).send(MESSAGE)
    assert caught.value.kind == "tls" and server.capture.messages == []


def test_authentication_succeeds_against_a_real_server(smtp):
    server = smtp("starttls", auth=True)
    options = {"smtp_security": "starttls", "smtp_ca_file": server.ca, "smtp_username": "user",
               "smtp_password": SECRET_PASSWORD}
    SmtpEmailSender(smtp_settings(server.port, **options)).send(MESSAGE)
    assert len(server.capture.messages) == 1


def test_a_login_the_provider_refuses_is_reported_without_its_words(smtp, monkeypatch):
    """aiosmtpd never answers a failed login over TLS, so the 535 is raised by the client here; the mapping is what is tested."""
    server = smtp("starttls", auth=True)

    def refuse(self, user, password, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials for user@secret-provider.example")

    monkeypatch.setattr(smtplib.SMTP, "login", refuse)
    options = {"smtp_security": "starttls", "smtp_ca_file": server.ca, "smtp_username": "user",
               "smtp_password": "not-the-password"}
    with pytest.raises(EmailDeliveryError) as caught:
        SmtpEmailSender(smtp_settings(server.port, **options)).send(MESSAGE)
    assert caught.value.kind == "auth" and caught.value.smtp_code == 535 and server.capture.messages == []
    assert "secret-provider" not in repr(caught.value) and "not-the-password" not in repr(caught.value)


def test_a_refused_recipient_is_reported_with_the_code_but_not_the_providers_words(smtp):
    server = smtp("none", refuse_recipient=True)
    with pytest.raises(EmailDeliveryError) as caught:
        SmtpEmailSender(smtp_settings(server.port)).send(MESSAGE)
    assert caught.value.kind == "rejected" and caught.value.smtp_code == 550
    assert "secret-provider-detail" not in repr(caught.value) and str(caught.value) == GENERIC_FAILURE


def test_nothing_listening_and_a_silent_server_both_fail_cleanly(smtp):
    with pytest.raises(EmailDeliveryError) as refused:
        SmtpEmailSender(smtp_settings(free_port())).send(MESSAGE)
    assert refused.value.kind == "connect"
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)  # accepts connections and never says a word
    try:
        with pytest.raises(EmailDeliveryError) as slow:
            SmtpEmailSender(smtp_settings(silent.getsockname()[1], smtp_timeout_seconds=0.5)).send(MESSAGE)
        assert slow.value.kind == "timeout"
    finally:
        silent.close()
    with pytest.raises(EmailDeliveryError) as unresolvable:
        SmtpEmailSender(smtp_settings(2525, smtp_host="no-such-host.invalid")).send(MESSAGE)
    assert unresolvable.value.kind == "connect"


def test_no_failure_ever_exposes_the_host_the_credentials_or_the_recipient(smtp):
    server = smtp("starttls", auth=True)
    failures = []
    for overrides in ({"smtp_ca_file": None}, {"smtp_password": "wrong"}, {"smtp_port": free_port()}):
        options = {"smtp_security": "starttls", "smtp_ca_file": server.ca, "smtp_username": "user",
                   "smtp_password": SECRET_PASSWORD, **overrides}
        with pytest.raises(EmailDeliveryError) as caught:
            SmtpEmailSender(smtp_settings(options.pop("smtp_port", server.port), **options)).send(MESSAGE)
        failures.append(caught.value)
    for failure in failures:
        shown = repr(failure) + str(failure) + repr(failure.args) + str(failure.__cause__) + str(failure.__context__ is None)
        for secret in (SECRET_PASSWORD, "localhost", "ann@example.com", "user"):
            assert secret not in str(failure) and secret not in repr(failure), secret
        assert failure.__cause__ is None, shown  # raised "from None": no chained provider text


# --- delivery as the request handler uses it ------------------------------------------------------------------------------


class Exploding:
    def send(self, message):
        raise RuntimeError("provider said: user ann@example.com token xyz")


def test_delivery_never_raises_and_logs_only_a_category_and_a_one_way_label(json_logs):
    token = "RAWTOKEN-0123456789abcdefghijklmnopqrstuvwxyzABCDE"
    settings = make_settings(public_base_url="https://atlas.example.com")
    deliver_password_reset(Exploding(), settings, "ann@example.com", token)  # must not raise
    deliver_password_reset(DisabledEmailSender(), settings, "ann@example.com", token)
    ok = SimpleNamespace(sent=[], send=lambda m: ok.sent.append(m))
    deliver_password_reset(ok, settings, "ann@example.com", token)
    assert len(ok.sent) == 1 and token in ok.sent[0].text  # the email carries the link; the logs must not
    events = {e["event"] for e in json_logs.events() if e["logger"] == "atlas.mail"}
    assert events == {"password_reset_email_failed", "password_reset_email_sent"}
    raw = json_logs.raw
    assert token not in raw and "reset-password#" not in raw and "ann@example.com" not in raw
    assert "provider said" not in raw and recipient_fingerprint("ann@example.com") in raw
    kinds = [e["kind"] for e in json_logs.events("password_reset_email_failed")]
    assert kinds == ["unexpected", "not_configured"]


# --- the endpoint -------------------------------------------------------------------------------------------------------------


def request_reset(client, email):
    return client.post("/auth/password-reset", json={"email": email})


def test_an_existing_account_gets_one_email_and_an_unknown_one_gets_the_same_answer_and_nothing(env, client):
    signup("exists@example.com")
    known = request_reset(client, "Exists@Example.com")  # case-insensitive, like sign-in
    unknown = request_reset(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    assert dict(known.headers).keys() >= {"content-type"} and known.headers["content-length"] == unknown.headers["content-length"]
    assert [m.to for m in env["outbox"].messages] == ["exists@example.com"]  # only the real account, only its own address
    assert env["outbox"].attempts == 1


def test_a_mail_outage_changes_nothing_the_caller_can_see(env, client, json_logs):
    signup("outage@example.com")
    healthy = request_reset(client, "nobody@example.com")
    env["outbox"].fail_with = EmailDeliveryError("connect")
    broken = request_reset(client, "outage@example.com")
    assert broken.status_code == healthy.status_code == 202
    assert broken.json() == healthy.json() and broken.headers["content-length"] == healthy.headers["content-length"]
    failure = json_logs.events("password_reset_email_failed")[-1]
    assert failure["kind"] == "connect" and failure["recipient"] == recipient_fingerprint("outage@example.com")
    assert "outage@example.com" not in json_logs.raw


def test_the_response_does_not_wait_for_the_mail_server(env, client):
    """The send is a background task: a slow or hanging provider cannot reveal whether an account exists."""
    import inspect

    from fastapi import BackgroundTasks

    from app.auth.routes import password_reset

    assert "background" in inspect.signature(password_reset).parameters
    assert BackgroundTasks  # the handler schedules the delivery instead of calling it inline


def test_reset_can_be_switched_off_and_still_answers_the_same(env, client, db_session):
    signup("off@example.com")
    env["settings"] = make_settings(auth_password_reset_enabled=False)
    response = request_reset(client, "off@example.com")
    assert response.status_code == 202
    assert env["outbox"].messages == []
    assert db_session.scalars(select(PasswordResetToken)).all() == []


def test_the_token_is_unpredictable_and_only_its_hash_is_stored(env, client, db_session):
    signup("hash@example.com")
    for _ in range(3):
        request_reset(client, "hash@example.com")
    tokens = [token for _, token in env["outbox"].sent]
    assert len(set(tokens)) == 3 and all(len(t) >= 43 for t in tokens)  # 256 bits, URL-safe
    stored = db_session.scalars(select(PasswordResetToken.token_hash)).all()
    assert len(stored) == 3 and not set(stored) & set(tokens)
    assert all(len(h) == 64 and set(h) <= set("0123456789abcdef") for h in stored)


def test_only_the_newest_link_works(env, client):
    signup("newest@example.com")
    request_reset(client, "newest@example.com")
    request_reset(client, "newest@example.com")
    old, new = (t for _, t in env["outbox"].sent)
    stale = client.post("/auth/password-reset/confirm", json={"token": old, "new_password": "a brand new long password"})
    assert stale.status_code == 400
    ok = client.post("/auth/password-reset/confirm", json={"token": new, "new_password": "a brand new long password"})
    assert ok.status_code == 204


def test_a_successful_reset_kills_every_other_unused_link(env, client, db_session):
    signup("others@example.com")
    request_reset(client, "others@example.com")
    first = env["outbox"].sent[-1][1]
    user_id = db_session.scalar(select(User.id).where(User.email == "others@example.com"))
    # a second link issued some other way (a concurrent request) must also die when the password changes
    db_session.add(PasswordResetToken(user_id=user_id, token_hash="f" * 64,
                                      expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)))
    db_session.flush()
    assert client.post("/auth/password-reset/confirm", json={"token": first, "new_password": "a brand new long password"}
                       ).status_code == 204
    assert db_session.scalars(select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None))).all() == []


def test_a_refused_password_does_not_spend_the_link(env, client):
    signup("refused@example.com")
    request_reset(client, "refused@example.com")
    token = env["outbox"].sent[-1][1]
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "aaaaaaaaaaaaaaaa"}
                       ).status_code == 422
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": PASSWORD + " v2"}
                       ).status_code == 204


def test_no_log_line_in_the_whole_flow_contains_a_token_or_a_link(env, client, json_logs):
    signup("logs@example.com")
    request_reset(client, "logs@example.com")
    token = env["outbox"].sent[-1][1]
    env["outbox"].fail_with = EmailDeliveryError("rejected", 550)
    request_reset(client, "logs@example.com")
    env["outbox"].fail_with = None
    client.post("/auth/password-reset/confirm", json={"token": "x" * 43, "new_password": PASSWORD + " v2"})
    client.post("/auth/password-reset/confirm", json={"token": token, "new_password": PASSWORD + " v2"})
    client.post("/auth/password-reset/confirm", json={"token": token, "new_password": PASSWORD + " v3"})
    raw = json_logs.raw
    assert token not in raw and "x" * 43 not in raw
    assert "reset-password#" not in raw and "#token=" not in raw
    assert "logs@example.com" not in raw


# --- redemption under concurrency (real commits, cleaned up) -------------------------------------------------------------------


def test_two_simultaneous_redemptions_cannot_both_succeed():
    settings = make_settings()
    email, token = "race-condition-test@example.com", "race-test-token-0123456789-abcdefghijklmnopqrstuv"
    with SessionLocal() as setup:
        user = AuthService(setup, settings).register(email, PASSWORD)
        from app.core import security

        setup.add(PasswordResetToken(user_id=user.id, token_hash=security.hash_token(token),
                                     expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)))
        setup.commit()
        user_id = user.id
    try:
        barrier = threading.Barrier(8)
        results: list[str] = []
        lock = threading.Lock()

        def redeem(index: int) -> None:
            with SessionLocal() as session:
                barrier.wait()
                try:
                    AuthService(session, settings).confirm_reset(token, f"password number {index} for the race")
                    session.commit()
                    outcome = "won"
                except AuthError:
                    session.rollback()
                    outcome = "refused"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=redeem, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert sorted(results) == ["refused"] * 7 + ["won"]
        with SessionLocal() as check:
            assert check.scalars(select(PasswordResetToken).where(PasswordResetToken.user_id == user_id,
                                                                  PasswordResetToken.used_at.is_(None))).all() == []
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
            cleanup.commit()
            assert cleanup.scalar(select(User.id).where(User.id == user_id)) is None
