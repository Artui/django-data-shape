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


class Session(models.Model):
    """A child whose parent is a plain fan-out target."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sessions")
    label = models.CharField(max_length=50)


class OptionalChild(models.Model):
    """A child whose foreign key may be null, for the null share."""

    company = models.ForeignKey(Company, null=True, on_delete=models.SET_NULL)
    label = models.CharField(max_length=50)


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


class Subscriber(models.Model):
    """A model with a single-column unique constraint.

    The arithmetic this makes checkable: a constant cannot fill a unique column
    more than once, and neither can a skew with fewer values than rows.
    """

    email = models.CharField(max_length=100, unique=True)


class Left(models.Model):
    """Half of a mutually-referencing pair, for the load-order cycle check."""

    right = models.ForeignKey("Right", null=True, on_delete=models.SET_NULL)


class Right(models.Model):
    """The other half. Neither can be loaded first, which is the point."""

    left = models.ForeignKey(Left, null=True, on_delete=models.SET_NULL)


class Tenant(models.Model):
    """A model keyed by UUID, which is common enough to be a first-class case."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)


class TenantRecord(models.Model):
    """A child of a UUID-keyed parent.

    The case that proves the key work reaches further than the key column: a
    foreign key over a UUID parent has to carry UUIDs, both out of the parent's
    real keys and into the child's own column.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="records")
    label = models.CharField(max_length=50)


class Reading(models.Model):
    """A model keyed by a timestamp, which needs its key prepared like any value.

    Exotic, and deliberately so: it is the smallest model that shows why the
    primary key goes through the field's own preparation rather than straight to
    the driver. A naive datetime handed to psycopg is stored verbatim; handed to
    Django first it is localised, which is what save() would have written.
    """

    at = models.DateTimeField(primary_key=True)
    value = models.IntegerField()


class Catalogue(models.Model):
    """A table nothing else in the suite builds.

    The one model reserved for the session-scoped fixture, because a world built
    once for the session outlives every test in it: any other test that built
    this table would meet rows it did not put there, and the refusal it got
    would be right and useless. Reserving a model is cheaper than ordering the
    suite around one fixture.
    """

    name = models.CharField(max_length=50)


class Account(models.Model):
    """A parent whose own columns a child derives from.

    Its own model rather than a couple of columns bolted onto ``Company``,
    because ``signed_up_at`` and ``plan`` would have to be nullable there not to
    break every existing declaration -- and a nullable parent column is exactly
    the case a derivation reading across the edge must not be tested against.
    """

    signed_up_at = models.DateTimeField()
    plan = models.CharField(max_length=20)


class Ticket(models.Model):
    """A child carrying all four faces of the derivation mechanism at once.

    One model rather than four, because the point being tested is that they are
    one mechanism: ``total`` reads two columns of its own row, one of which is
    itself derived, so a declaration whose computation order followed the column
    order would compute it from an empty slot.
    """

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="tickets")
    opened_at = models.DateTimeField()
    severity = models.CharField(max_length=20)
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)


class ActiveVendorManager(models.Manager):
    """A default manager that hides rows, which is an ordinary thing to write."""

    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(retired=False)


class Vendor(models.Model):
    """A parent whose default manager hides some of its own rows.

    Here because a fan-out reads every parent and a derivation reads their
    columns, and those two reads must agree about which parents exist. A default
    manager is the ordinary way a project makes them disagree.
    """

    name = models.CharField(max_length=50)
    retired = models.BooleanField(default=False)

    objects = ActiveVendorManager()


class Supply(models.Model):
    """A child of a partly-hidden parent, carrying one of its columns."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    vendor_name = models.CharField(max_length=50)
