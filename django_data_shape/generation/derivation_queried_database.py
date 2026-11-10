from __future__ import annotations


class DerivationQueriedDatabase(Exception):
    """Callable code supplied to a shape queried the database while generating.

    Its own type: this package may call your code, but your code may not query the database."""
