"""Version-4 shaped UUID keys, derived rather than random."""

from __future__ import annotations

import hashlib
import uuid


class UuidKeys:
    """A UUID per row, deterministic in the seed and the row index.

    Derived from a hash rather than drawn from ``uuid4`` for the reason the
    whole package is built around: two builds of one shape have to agree, and a
    random key would make the primary key -- and therefore every foreign key
    pointing at it -- differ between runs.

    A full 128 bits from the digest, not a float draw. A draw carries 53 bits,
    which sounds ample until birthday collisions arrive around ninety million
    rows; a table that large is exactly the kind this package exists to build.

    The version and variant bits are stamped so the result is a well-formed
    version 4 UUID. Applications store v4 keys, so a test database holding
    something that merely looks UUID-shaped would be unlike the thing it stands
    in for.
    """

    def key_for(self, row: int, stream: int) -> object:
        digest = hashlib.blake2b(
            stream.to_bytes(8, "big") + row.to_bytes(8, "big"), digest_size=16
        ).digest()
        raw = bytearray(digest)
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return uuid.UUID(bytes=bytes(raw))

    def is_disjoint_from_existing_rows(self) -> bool:
        """Always. A 128-bit digest cannot land on a caller's row. See ``Disjoint``.

        The full digest is what makes this a statement rather than a hope. A
        draw carries 53 bits, where birthday collisions arrive around ninety
        million rows; these are 122 bits after the version and variant are
        stamped, which is the space a version 4 UUID has and the space every
        application storing one is already relying on.
        """
        return True

    def canonical(self) -> object:
        """Nothing to say: the digest is a function of the seed and the row. See ``Canonical``."""
        return ()

    def __repr__(self) -> str:
        return "UuidKeys()"
