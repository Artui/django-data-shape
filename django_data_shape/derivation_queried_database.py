"""Raised when generating a row ran a query."""

from __future__ import annotations


class DerivationQueriedDatabase(Exception):
    """Callable code supplied to a shape queried the database while generating.

    Its own type because the rule it enforces is this package's boundary rather
    than a detail of one declaration: **this package may call your code, but
    your code may not call the database.**

    The most likely feature request this package will ever receive is a per-row
    creation hook -- "call my service to build each object" -- and it is
    declined permanently. ``Model.objects.create()`` per row is the thing being
    replaced; offering it makes it the default path, because it is the easiest
    thing to write, and a package whose default path is not ``COPY`` has no
    reason to exist.

    What makes the refusal worth stating as a rule rather than as advice is that
    it is decidable. The generation pass runs under a wrapper on the connection
    being built, so a query raises this rather than quietly costing a round trip
    per row -- a fact rather than a convention.

    The check sees queries on **the connection being built**. Code that reaches
    a different alias, or another thread's connection, is outside what a wrapper
    on one connection can observe; the rule still holds there, and only its
    enforcement stops at the edge of the connection.
    """
