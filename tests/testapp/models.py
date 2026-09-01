"""Models the shape vocabulary is exercised against.

Deliberately a small graph rather than one table: a shape declaration only earns
its keep over an ordinary loop once there are edges, so the suite's own fixture
schema has them even though this release refuses to shape them.
"""

from __future__ import annotations

import uuid

from django.db import models


class Company(models.Model):
    """A plain table with nothing optional about it."""

    name = models.CharField(max_length=200)


class Order(models.Model):
    """One table covering every kind of column the validator reasons about.

    ``note`` is nullable and ``channel`` has a default, so both are legitimately
    undeclarable; ``status``, ``total`` and ``created_at`` are none of those
    things and must be declared. That split is the whole of what makes a field
    required to a shape, so the fixture model exists to have one of each.
    """

    status = models.CharField(max_length=20)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField()
    note = models.TextField(null=True)
    channel = models.CharField(max_length=20, default="web")


class Project(models.Model):
    """A fan-out child carrying a per-group invariant.

    Nothing in this release can shape it -- a foreign key is refused -- and that
    refusal is exactly what one of the tests asserts. It is declared now rather
    than later because the constraint below is the worked example the invariant
    work is designed against: at most one project per company may be active,
    which is what makes the marginal distribution of ``status`` a derived
    quantity rather than a declarable one.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="projects")
    status = models.CharField(max_length=20)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(status="ACTIVE"),
                name="one_active_project_per_company",
            ),
        ]


class Reserved(models.Model):
    """A model whose column name collides with Table's own signature.

    Not a contrived case: ``rows`` is an ordinary word for a column. Python
    binds the keyword to Table's row count before the field ever sees it, so
    without the ``fields=`` mapping this model would be undeclarable.
    """

    rows = models.IntegerField()


class Defaulted(models.Model):
    """A model whose default is a callable, which cannot be guessed at.

    ``uuid4`` produces a different value per row and ``dict`` produces the same
    one every time; nothing on the field distinguishes them, so a shape has to
    be told rather than left to pick a reading.
    """

    token = models.UUIDField(default=uuid.uuid4)


class Event(models.Model):
    """A model for checking that field preparation actually happens.

    ``at`` is the interesting column: written without Django's own preparation a
    naive datetime lands in the database verbatim, which under a non-UTC
    ``TIME_ZONE`` is hours away from where ``save()`` would have put it. ``tags``
    cannot be written at all without preparation, because psycopg has no adapter
    for a bare dict.
    """

    at = models.DateTimeField()
    tags = models.JSONField()


class SlugPk(models.Model):
    """A model whose primary key this package cannot assign.

    Keys are handed out as a dense 1..N integer range, and nothing converts
    them, so a character primary key used to be loaded with the strings "1",
    "2", "3".
    """

    code = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=50)


class Referred(models.Model):
    """A model with an optional self-relation, which may be left undeclared."""

    label = models.CharField(max_length=50)
    referrer = models.ForeignKey("self", null=True, on_delete=models.SET_NULL)
