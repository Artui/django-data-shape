from __future__ import annotations

import hashlib
import uuid


class UuidKeys:
    """A UUID per row, deterministic in the seed and the row index.

    Derived from a hash, not ``uuid4``: two builds of one shape have to agree."""

    def key_for(self, row: int, stream: int) -> object:
        digest = hashlib.blake2b(
            stream.to_bytes(8, "big") + row.to_bytes(8, "big"), digest_size=16
        ).digest()
        raw = bytearray(digest)
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return uuid.UUID(bytes=bytes(raw))

    def is_disjoint_from_existing_rows(self) -> bool:
        """Always. A 128-bit digest cannot land on a caller's row. See ``Disjoint``."""
        return True

    def canonical(self) -> object:
        """Nothing to say: the digest is a function of the seed and the row. See ``Canonical``."""
        return ()

    def __repr__(self) -> str:
        return "UuidKeys()"
