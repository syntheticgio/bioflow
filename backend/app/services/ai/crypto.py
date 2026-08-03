"""API keys at rest.

Fernet, with the key in a file next to the database rather than in an
environment variable -- an env var means the key sits in `.env`, in the compose
config, and in `docker inspect` output.

**The honest scope of this:** the key file is on the same disk as the Mongo
data, so anyone with shell access to this machine has both and can decrypt
everything. What it defends against is a look at the collection -- an opened
Compass window, a stray `mongodump` in a backup. That is the threat this tool
has, and the settings page says so rather than implying more.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


def key_path() -> Path:
    """Where the encryption key lives.

    Under `.biopipe/`, which already holds the mount sentinel and the lock
    file, so this needs no new directory and follows a relocated BIOINFO_HOME.
    """
    return settings.bioinfo_home / ".biopipe" / "secret.key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """The cipher, generating the key file on first use.

    Cached because reading the file per call would be per-request filesystem IO
    for a value that cannot change while the process runs. Tests clear it.
    """
    path = key_path()
    if path.exists():
        return Fernet(path.read_bytes())

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Written 0600 from the start rather than chmod-ed after: a world-readable
    # window, however short, is the kind of thing that survives into a backup.
    path.touch(mode=0o600, exist_ok=True)
    path.write_bytes(key)
    log.info("ai_key_file_created", path=str(path))
    return Fernet(key)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str | None:
    """The plaintext key, or None if it cannot be decrypted.

    None rather than an exception because the realistic cause is a key file
    that was deleted or replaced, and the useful response to that is a settings
    page saying the key needs re-entering -- not a 500 on every page load.
    """
    try:
        return _fernet().decrypt(token).decode()
    except (InvalidToken, ValueError, TypeError):
        log.warning("ai_key_undecryptable")
        return None


def hint(plaintext: str) -> str | None:
    """The masked form shown in the UI: prefix, ellipsis, last four.

    Short strings are masked entirely. A key short enough that "all but the
    last four" would show most of it is a key this must not partially print.
    The prefix length depends on the branch (7 for "sk-", 3 otherwise), so the
    length floor below is computed from that prefix rather than a single fixed
    constant -- otherwise a threshold tuned for the 3-char generic prefix would
    let the 7-char "sk-" prefix through with barely anything actually masked.
    """
    if not plaintext:
        return None
    prefix_len = 7 if plaintext.startswith("sk-") else 3
    # Require at least 4 genuinely-masked characters in the middle, on top of
    # the prefix and the last-four suffix, before showing the partial form.
    if len(plaintext) < prefix_len + 4 + 4:
        return "…"
    prefix = plaintext[:prefix_len]
    return f"{prefix}…{plaintext[-4:]}"
