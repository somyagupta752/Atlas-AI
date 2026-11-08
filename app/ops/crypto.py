"""Authenticated, chunked file encryption for backups (AES-256-GCM).

Format: ``ATLASBK1`` | 8-byte random nonce prefix | chunks of ``[4-byte length][ciphertext+tag]``.
Each chunk's nonce is the prefix plus a counter, and its associated data names the counter and whether it is the
last chunk, so reordering, truncation and tampering are detected. Memory use is one chunk, not the file.
"""

import base64
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ATLASBK1"
CHUNK = 1024 * 1024


class BackupCryptoError(Exception):
    pass


def parse_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded.strip()) % 4))
    except ValueError:
        raise BackupCryptoError("The backup key is not valid base64") from None
    if len(key) != 32:
        raise BackupCryptoError("The backup key must decode to 32 bytes (generate one with `python -m app.ops.backup keygen`)")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">I", counter)


def _aad(counter: int, last: bool) -> bytes:
    return struct.pack(">I?", counter, last)


def encrypt_file(source: Path, target: Path, key: bytes) -> None:
    aes, prefix = AESGCM(key), os.urandom(8)
    size = source.stat().st_size
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with source.open("rb") as src, os.fdopen(fd, "wb") as out:
        out.write(MAGIC + prefix)
        counter, done = 0, 0
        while True:
            block = src.read(CHUNK)
            done += len(block)
            last = done >= size
            sealed = aes.encrypt(_nonce(prefix, counter), block, _aad(counter, last))
            out.write(struct.pack(">I", len(sealed)) + sealed)
            counter += 1
            if last:
                break


def decrypt_file(source: Path, target: Path, key: bytes) -> None:
    aes = AESGCM(key)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with source.open("rb") as src, os.fdopen(fd, "wb") as out:
        header = src.read(len(MAGIC) + 8)
        if len(header) != len(MAGIC) + 8 or header[:len(MAGIC)] != MAGIC:
            raise BackupCryptoError("This is not an Atlas backup file")
        prefix, counter = header[len(MAGIC):], 0
        saw_last = False
        while True:
            raw = src.read(4)
            if not raw:
                break
            if saw_last or len(raw) != 4:
                raise BackupCryptoError("The backup file is damaged")
            (length,) = struct.unpack(">I", raw)
            sealed = src.read(length)
            if len(sealed) != length:
                raise BackupCryptoError("The backup file is truncated")
            # The last chunk is the one with nothing after it. Try that reading first at end of file.
            at_end = not src.peek(1) if hasattr(src, "peek") else src.tell() == source.stat().st_size
            try:
                out.write(aes.decrypt(_nonce(prefix, counter), sealed, _aad(counter, at_end)))
            except InvalidTag:
                raise BackupCryptoError("The backup is corrupt, truncated, or the key is wrong") from None
            saw_last = at_end
            counter += 1
        if not saw_last:
            raise BackupCryptoError("The backup file is truncated")
