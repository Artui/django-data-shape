from __future__ import annotations

import hashlib
import uuid


class Md5Keys:
    """A version 4 shaped UUID per row, derived in Python **and** in SQL.

    The projection half of ``UuidKeys``, as its own strategy rather than a second meaning."""

    def key_for(self, row: int, stream: int) -> object:
        return uuid.UUID(bytes=bytes(_stamp(_digest(stream, row))))

    def key_sql(self, stream: int, row: str) -> str:
        """The same digest, stamped the same way, computed by the server."""
        digest = f"md5(decode('{stream:016x}' || lpad(to_hex(({row})::bigint), 16, '0'), 'hex'))"
        variant = f"to_hex((('x' || substr({digest}, 17, 1))::bit(4)::int & 3) | 8)"
        return (
            f"(overlay(overlay({digest} placing '4' from 13 for 1) "
            f"placing {variant} from 17 for 1))::uuid"
        )

    def is_disjoint_from_existing_rows(self) -> bool:
        """Always, for the reason ``UuidKeys`` gives. See ``Disjoint``."""
        return True

    def canonical(self) -> object:
        """Nothing to say: the digest is a function of the seed and the row. See ``Canonical``."""
        return ()

    def __repr__(self) -> str:
        return "Md5Keys()"


def _digest(stream: int, row: int) -> bytes:
    return hashlib.md5(  # noqa: S324
        stream.to_bytes(8, "big") + row.to_bytes(8, "big"), usedforsecurity=False
    ).digest()


def _stamp(digest: bytes) -> bytearray:
    """Make a raw digest a well-formed version 4 UUID, as applications store them."""
    raw = bytearray(digest)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return raw
