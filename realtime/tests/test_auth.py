"""Authentication of WebSocket clients: dev token parsing and the DEV_AUTH guard."""

import importlib

import pytest

from app import config
from app.auth import AuthError, DevAuthenticator, Principal, Role, get_authenticator


@pytest.fixture
def dev_auth(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)


@pytest.fixture
def reload_config(monkeypatch):
    """Re-imports config after the test changed the environment, then restores it."""
    yield lambda: importlib.reload(config)
    monkeypatch.undo()
    importlib.reload(config)


class TestDevAuthenticator:
    @pytest.mark.parametrize("role", list(Role))
    def test_token_gives_user_id_and_role(self, dev_auth, role):
        principal = DevAuthenticator().authenticate(f"dev:{role}:42")

        assert principal == Principal(user_id=42, role=role)

    def test_largest_user_id_fits_a_64_bit_integer(self, dev_auth):
        principal = DevAuthenticator().authenticate("dev:driver:" + "9" * 18)

        assert principal.user_id < 2**63

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "garbage",
            "header.payload.signature",
            "dev:driver",
            "dev:driver:",
            "dev:driver:42:extra",
            "dev:root:1",
            "dev:Driver:1",
            "dev:driver:0",
            "dev:driver:042",
            "dev:driver:-1",
            "dev:driver:+1",
            "dev:driver:4.2",
            "dev:driver:" + "1" * 19,
            "dev:driver:٤٢",
            " dev:driver:42",
            "dev:driver:42 ",
            "dev:driver:42\n",
        ],
        ids=[
            "empty",
            "garbage",
            "jwt-like",
            "no-user-id",
            "empty-user-id",
            "extra-part",
            "unknown-role",
            "role-case",
            "zero-user-id",
            "leading-zero",
            "negative",
            "plus-sign",
            "fraction",
            "too-long",
            "non-ascii-digits",
            "leading-space",
            "trailing-space",
            "trailing-newline",
        ],
    )
    def test_malformed_token_is_rejected(self, dev_auth, token):
        with pytest.raises(AuthError, match="malformed"):
            DevAuthenticator().authenticate(token)

    def test_valid_token_is_rejected_when_dev_auth_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "DEV_AUTH", False)

        with pytest.raises(AuthError, match="disabled"):
            DevAuthenticator().authenticate("dev:driver:42")

    def test_get_authenticator_returns_the_dev_authenticator(self):
        assert isinstance(get_authenticator(), DevAuthenticator)


class TestDevAuthFlag:
    def test_is_on_only_for_exactly_one(self, monkeypatch, reload_config):
        monkeypatch.setenv("DEV_AUTH", "1")

        assert reload_config().DEV_AUTH is True

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", " 1"])
    def test_is_off_for_any_other_value(self, monkeypatch, reload_config, value):
        monkeypatch.setenv("DEV_AUTH", value)

        assert reload_config().DEV_AUTH is False

    def test_is_off_when_not_set(self, monkeypatch, reload_config):
        monkeypatch.delenv("DEV_AUTH", raising=False)

        assert reload_config().DEV_AUTH is False
