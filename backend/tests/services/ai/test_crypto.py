"""The key file and the round trip.

No `beanie_models` here: this touches the filesystem and nothing else.
"""

import stat

import pytest

from app.services.ai import crypto


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    """Point crypto at a throwaway BIOINFO_HOME."""
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield tmp_path
    crypto._fernet.cache_clear()


class TestKeyFile:
    def test_creates_the_key_file_on_first_use(self, key_dir):
        crypto.encrypt("sk-test")
        assert crypto.key_path().exists()

    def test_key_file_is_owner_read_write_only(self, key_dir):
        crypto.encrypt("sk-test")
        mode = stat.S_IMODE(crypto.key_path().stat().st_mode)
        assert mode == 0o600

    def test_reuses_an_existing_key(self, key_dir):
        """Regenerating would silently make every stored key undecryptable."""
        token = crypto.encrypt("sk-test")
        first = crypto.key_path().read_bytes()
        crypto._fernet.cache_clear()
        assert crypto.decrypt(token) == "sk-test"
        assert crypto.key_path().read_bytes() == first


class TestRoundTrip:
    def test_round_trips(self, key_dir):
        assert crypto.decrypt(crypto.encrypt("sk-ant-secret")) == "sk-ant-secret"

    def test_ciphertext_does_not_contain_the_plaintext(self, key_dir):
        """The whole point: a look at the Mongo collection shows nothing."""
        assert b"secret" not in crypto.encrypt("sk-ant-secret")

    def test_decrypt_returns_none_on_garbage(self, key_dir):
        """A key encrypted under a lost key file must not crash the settings
        page -- it reads as a provider whose key needs re-entering."""
        assert crypto.decrypt(b"not-a-fernet-token") is None


class TestHint:
    def test_hint_masks_all_but_the_last_four(self):
        assert crypto.hint("sk-ant-api03-abcdefgh4f2a") == "sk-ant-…4f2a"

    def test_short_keys_are_fully_masked(self):
        """Never leak a short key by showing most of it."""
        assert crypto.hint("abc") == "…"

    def test_hint_of_empty_is_none(self):
        assert crypto.hint("") is None
