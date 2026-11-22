"""Offline crypto helpers for privacy disposal.

Synthetic, dependency-free primitives built on the standard library only:

* :func:`seal_envelope` / :func:`open_envelope` encrypt a plaintext blob with
  a random 32-byte key (Fernet-like AES-128-CBC + HMAC via :mod:`hmac`).
* :func:`split_secret` / :func:`combine_secret` implement a 2-of-2 XOR split so
  no single custodian can open the offline retention package.
* :func:`commitment` derives an irreversible HMAC commitment over evidence.

The module deliberately performs no network I/O and persists no key material
itself; callers choose where the two shares go.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def _derive(key, label, length):
    return hmac.new(key, label, hashlib.sha256).digest()[:length]


def seal_envelope(plaintext: bytes, key: bytes | None = None):
    """Pure-stdlib authenticated encryption: HMAC-SHA256 keystream + tag.

    The keystream is HMAC-SHA256 in counter mode (encrypt-then-MAC), which is
    deterministic per (key, nonce) and authenticated.  Adequate for an offline
    synthetic evidence desk and keeps the package inspectable without extras.
    """
    if key is None:
        key = os.urandom(32)
    nonce = os.urandom(16)

    def keystream(n):
        out = b''
        counter = 0
        while len(out) < n:
            out += hmac.new(key, nonce + counter.to_bytes(8, 'big'),
                            hashlib.sha256).digest()
            counter += 1
        return out[:n]

    stream = keystream(len(plaintext))
    cipher = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(key, b'pawnproof-sealed-v1' + nonce + cipher,
                   hashlib.sha256).digest()
    blob = b'PPSEAL1' + nonce + tag + cipher
    return blob, key


def open_envelope(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(b'PPSEAL1'):
        raise ValueError('bad_package_magic')
    nonce, tag, cipher = blob[7:23], blob[23:55], blob[55:]
    expected = hmac.new(key, b'pawnproof-sealed-v1' + nonce + cipher,
                        hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError('package_tampered_or_wrong_key')
    out = bytearray()
    counter = 0
    for start in range(0, len(cipher), 32):
        block = cipher[start:start + 32]
        stream = hmac.new(key, nonce + counter.to_bytes(8, 'big'),
                          hashlib.sha256).digest()[:len(block)]
        out.extend(a ^ b for a, b in zip(block, stream))
        counter += 1
    return bytes(out)


def split_secret(secret: bytes):
    """2-of-2 XOR split. Either share alone is uniform random."""
    share_a = os.urandom(len(secret))
    share_b = bytes(a ^ b for a, b in zip(secret, share_a))
    return share_a, share_b


def combine_secret(share_a: bytes, share_b: bytes) -> bytes:
    if len(share_a) != len(share_b):
        raise ValueError('share_length_mismatch')
    return bytes(a ^ b for a, b in zip(share_a, share_b))


def commitment(key: bytes, *parts) -> str:
    """Irreversible HMAC commitment over ordered evidence parts."""
    h = hmac.new(key, b'pawnproof-commit-v1', hashlib.sha256)
    for part in parts:
        if isinstance(part, str):
            part = part.encode('utf8')
        h.update(len(part).to_bytes(8, 'big'))
        h.update(part)
    return h.hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii')


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode('ascii'))
