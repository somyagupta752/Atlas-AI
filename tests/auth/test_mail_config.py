"""15.5: the reset email settings fail closed. A deployment cannot start with password reset on and no way to deliver it."""

import pytest

from app.config import Settings

DEPLOYED = {
    "database_url": "postgresql://u:Zq8rT5vN2mKp4LxW@db.internal/x", "auth_secret_key": "x7Kq9vLm2RtZp4NwB8cYd3FhJs6Ua5Ge",
    "patch_test_backend": "container", "patch_container_allow_untrusted": True, "patch_workspace_dir": "/w",
}
MAIL = {"smtp_host": "smtp.example.com", "smtp_from": "atlas@example.com", "public_base_url": "https://atlas.example.com",
        "smtp_username": "user", "smtp_password": "provider-password-123"}


def build(environment="production", **overrides):
    return Settings(_env_file=None, environment=environment, **{**DEPLOYED, **MAIL, **overrides})


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_complete_mail_configuration_starts(environment):
    settings = build(environment)
    assert settings.smtp_security == "starttls" and settings.smtp_password.get_secret_value() == "provider-password-123"
    assert "provider-password-123" not in repr(settings) and "provider-password-123" not in str(settings.public_summary())


@pytest.mark.parametrize("missing, message", [
    ({"smtp_host": ""}, "SMTP_HOST is required"),
    ({"smtp_from": ""}, "SMTP_FROM is required"),
    ({"public_base_url": ""}, "PUBLIC_BASE_URL must be an https://"),
    ({"public_base_url": "http://atlas.example.com"}, "PUBLIC_BASE_URL must be an https://"),
    ({"smtp_security": "none"}, "SMTP_SECURITY must be"),
    ({"smtp_password": ""}, "SMTP_PASSWORD is required when SMTP_USERNAME is set"),
    ({"smtp_username": ""}, "SMTP_USERNAME is required when SMTP_PASSWORD is set"),
    ({"smtp_from": "not an address"}, "SMTP_FROM must be a plain email address"),
    ({"public_base_url": "https://atlas.example.com/app"}, "PUBLIC_BASE_URL must be an origin"),
])
def test_a_deployment_with_a_gap_in_the_mail_settings_refuses_to_start(missing, message):
    with pytest.raises(ValueError, match=message):
        build(**missing)


def test_a_relay_that_trusts_the_network_needs_no_login_but_still_needs_tls():
    assert build(smtp_username="", smtp_password="").smtp_username is None


def test_switching_reset_off_is_the_only_way_to_run_without_a_mail_server():
    off = build(smtp_host="", smtp_from="", public_base_url="", smtp_username="", smtp_password="",
                auth_password_reset_enabled=False)
    assert off.auth_password_reset_enabled is False and off.smtp_host is None


def test_development_needs_no_mail_server_but_still_rejects_nonsense():
    plain = Settings(_env_file=None, environment="development", database_url="postgresql://localhost/x")
    assert plain.smtp_host is None and plain.auth_password_reset_enabled
    with pytest.raises(ValueError, match="SMTP_FROM"):
        Settings(_env_file=None, environment="development", database_url="postgresql://localhost/x", smtp_from="x")
