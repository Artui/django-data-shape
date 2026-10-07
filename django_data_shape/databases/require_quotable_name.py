"""The refusal every database name this package writes into a statement goes through."""

from __future__ import annotations


def require_quotable_name(name: str, role: str, *, refusal: type[Exception] = ValueError) -> None:
    """Refuse a database name holding a double quote, before any statement is built from it.

    ``CREATE DATABASE``, ``DROP DATABASE`` and ``ALTER DATABASE`` take no bound
    parameters, so a database name reaches them through the connection's
    ``quote_name``, and Django's PostgreSQL one does two things that make a
    double quote unsafe. It wraps a name in double quotes and escapes nothing
    inside it, so ``a"b`` becomes a statement that does not parse -- or, with
    the right text after the quote, one that says more than the caller wrote.
    And it passes a name that already starts and ends with one through
    unchanged, so ``'"x"'`` is written as the database ``x``, while every place
    the same string goes as a value -- a lookup in ``pg_database``, or a
    connection's ``NAME`` -- means a database whose name holds the quotes. The
    statement and the check before it name two databases.

    ``drop_database`` is the sharpest case, because it does both: it looks the
    name up, to say whether it was there, and then drops ``IF EXISTS``. Given
    ``'"x"'`` it would report that nothing was there and drop ``x``. So every
    name taken from a caller is refused here first, and since no database this
    package makes is named that way, nothing it makes is refused.

    ``role`` says which name it was, the way the caller's own parameter does.
    ``refusal`` is the error to raise: ``ValueError`` by default, because what
    is wrong is an argument -- the reason
    :func:`~django_data_shape.backends.require_clone_strategy.require_clone_strategy`
    gives -- and :class:`~django_data_shape.databases.unusable_base.UnusableBase`
    from :func:`~django_data_shape.databases.template_database.template_database`,
    where the name is a base's and every other way a base cannot be started
    from raises that.
    """
    if '"' in name:
        raise refusal(
            f"The {role} name {name!r} contains a double quote. Django quotes a database "
            "name by wrapping it in double quotes, passes one that is already wrapped through "
            "unchanged and escapes nothing inside it, so a statement could name a different "
            "database from the one this name means where it is passed as a value -- a lookup "
            "in pg_database, or a connection's NAME. Use a name without one; a database "
            "already named so can be renamed in psql, writing each double quote inside the "
            "name twice."
        )
