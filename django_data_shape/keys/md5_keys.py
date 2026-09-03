"""UUID keys a database can compute for itself, so a projection can carry them."""

from __future__ import annotations

import hashlib
import uuid


class Md5Keys:
    """A version 4 shaped UUID per row, derived in Python **and** in SQL.

    The projection half of :class:`~django_data_shape.keys.uuid_keys.UuidKeys`,
    and a separate strategy rather than a second meaning for that one. The two
    produce different keys for the same row, so quietly making one become the
    other where a SQL twin is needed would change every key in every world
    already built, and would give one declaration two meanings depending on
    which statement filled the table.

    **Why md5 rather than blake2b.** A
    :class:`~django_data_shape.projection.Projection` has no declared row count,
    so its rows never pass through Python and its keys have to be assigned by
    the ``INSERT ... SELECT`` that writes them -- which means the hash has to
    exist on both sides and agree byte for byte. ``blake2b`` has no PostgreSQL
    equivalent; ``md5`` is in the standard library and is a built-in function of
    the server. That is the whole of the reason, and it is worth being plain
    that **this is not a security choice**: nothing here authenticates anything,
    the input is a table's own seed and row index, and md5's weakness is
    collision resistance against an adversary who chooses the input. Nobody
    chooses these inputs.

    128 bits, of which 122 survive the version and variant stamp -- the same
    space every application storing a v4 UUID already relies on, and far past
    where birthday collisions matter for a test database.

    ``usedforsecurity=False`` is passed because it has to be: on a FIPS build,
    ``hashlib.md5`` without it raises rather than returning a digest, which
    would make this strategy unusable on hosts that are otherwise fine.
    """

    def key_for(self, row: int, stream: int) -> object:
        return uuid.UUID(bytes=bytes(_stamp(_digest(stream, row))))

    def key_sql(self, stream: int, row: str) -> str:
        """The same digest, stamped the same way, computed by the server.

        The two halves are checked against each other rather than argued about:
        a test computes both for the same rows and compares them, which is the
        only form of agreement that means anything here.

        ``md5`` takes the same eight-byte big-endian pair Python hashes, built
        with ``to_hex``/``lpad`` and turned back into bytes by ``decode``, so
        neither side is hashing a rendering of the other's input. The stamp is
        two ``overlay`` calls on the hex digest: nibble 13 is the version and is
        always ``4``, and nibble 17 is the variant, which keeps its low two bits
        and takes ``8`` in the high two.
        """
        digest = (
            f"md5(decode(lpad(to_hex({stream}::bigint), 16, '0') || "
            f"lpad(to_hex(({row})::bigint), 16, '0'), 'hex'))"
        )
        variant = f"to_hex((('x' || substr({digest}, 17, 1))::bit(4)::int & 3) | 8)"
        return (
            f"(overlay(overlay({digest} placing '4' from 13 for 1) "
            f"placing {variant} from 17 for 1))::uuid"
        )

    def is_disjoint_from_existing_rows(self) -> bool:
        """Always, for the reason ``UuidKeys`` gives. See ``Disjoint``."""
        return True

    def canonical(self) -> object:
        """Nothing to say: the digest is a function of the seed and the row. See ``Canonical``.

        Empty like ``UuidKeys``' own, and not the same thing -- the strategy's
        type name is part of the digest, so two shapes differing only in which
        of them they use are two different worlds, which they are.
        """
        return ()

    def __repr__(self) -> str:
        return "Md5Keys()"


def _digest(stream: int, row: int) -> bytes:
    return hashlib.md5(  # noqa: S324
        stream.to_bytes(8, "big") + row.to_bytes(8, "big"), usedforsecurity=False
    ).digest()


def _stamp(digest: bytes) -> bytearray:
    """Make a raw digest a well-formed version 4 UUID.

    Applications store v4 keys, so a test database holding something that merely
    looked UUID-shaped would be unlike the thing it stands in for.
    """
    raw = bytearray(digest)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return raw
