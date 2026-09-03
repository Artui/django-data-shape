"""Models the shape vocabulary is exercised against.

Deliberately a small graph rather than one table: a shape declaration only earns
its keep over an ordinary loop once there are edges, so the suite's own fixture
schema has them even though this release refuses to shape them.
"""

from __future__ import annotations

import uuid

import django
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


class Prepared(models.Model):
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


class Template(models.Model):
    """The thing an event is created from, and the collection's real owner.

    The motivating graph for a projection, kept as its own four models rather
    than folded onto ``Company``: the point being tested is that a child
    collection's cardinality is *determined* by a table two edges away, and that
    only shows when both sides of the join exist.
    """

    name = models.CharField(max_length=50)


class TemplateSession(models.Model):
    """The collection that gets copied."""

    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="sessions")
    title = models.CharField(max_length=50)
    minutes = models.IntegerField()


class Venue(models.Model):
    """A second thing an event points at, so a join can be ambiguous."""

    name = models.CharField(max_length=50)


class Event(models.Model):
    """Created from a template, and the table a projection makes one row per."""

    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="events")
    venue = models.ForeignKey(Venue, null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=50)


class EventSession(models.Model):
    """One of each kind of column a derived projection has to decide about.

    ``event`` is the edge the rows hang off, ``source`` points back at the row
    that was copied, ``title`` and ``minutes`` match by name, ``channel`` has a
    plain default and ``note`` is nullable. That is the whole rule set in one
    model, which is what makes a single build able to falsify all of it.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="sessions")
    source = models.ForeignKey(TemplateSession, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=50)
    minutes = models.IntegerField()
    channel = models.CharField(max_length=20, default="web")
    note = models.TextField(null=True)


class Attendance(models.Model):
    """A fan-out child of a projected table.

    Here to answer the question a projection raises about load order: a table
    filled by a statement is still a table, so something else may fan out over
    it -- provided the ordering pass puts the projection first.
    """

    session = models.ForeignKey(EventSession, on_delete=models.CASCADE)
    name = models.CharField(max_length=50)


class Rehearsal(models.Model):
    """A collection joinable to an event through two different models.

    Template *and* venue, where ``TemplateSession`` shares only the template.
    Which collection is being copied is then genuinely undecidable, and guessing
    would build a different database from the one that was declared.
    """

    template = models.ForeignKey(Template, on_delete=models.CASCADE)
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class SparseSession(models.Model):
    """A projected model with a column nothing can fill.

    ``headcount`` is not null, has no default, and is not a column the copied
    collection carries under that name.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)
    headcount = models.IntegerField()


class TokenSession(models.Model):
    """A projected model with a callable default.

    Refused for the reason ``Table`` refuses one, and for a second reason on top
    of it: the rows are made by one statement and never pass through Python, so
    there is no per-row moment at which the callable could be called.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)
    token = models.UUIDField(default=uuid.uuid4)


class UuidSession(models.Model):
    """A projected model whose keys cannot be written in SQL."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class DualSession(models.Model):
    """A projected model with two edges into the table it is projected per.

    Which one the projected rows hang off is then a question the model graph
    cannot answer, and answering it by picking the first would build a different
    database from the one that was declared.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    replaces = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=50)


class AuditedSession(models.Model):
    """A projected table reserved for the statistics assertion.

    Its own model for the reason ``Catalogue`` has one, and a sharper version of
    it: ``pg_statistic`` rows are **not** rolled back and survive the truncation
    between transactional tests, so one test's ``ANALYZE`` of a shared model
    would make another test's assertion true without the code under test having
    done anything. Nothing else in the suite builds this table.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class Bucketed(models.Model):
    """A table reserved for the assertion that a declared statistics target lands.

    Its own model for the reason ``AuditedSession`` has one, and one turn
    sharper. ``ALTER TABLE ... SET STATISTICS`` is DDL that commits with the
    build, and neither it nor the ``pg_statistic`` rows it changes are rolled
    back between transactional tests -- so a target set by one test is still
    there for the next, and an assertion about a shared table would pass or fail
    on whichever test ran first.
    """

    code = models.CharField(max_length=20)


class Narrowed(models.Model):
    """A table reserved for the refusal that a target is what avoids.

    Separate from ``Bucketed`` for exactly the reason above: this one must meet
    the server's own default target, and a test that had raised the target on a
    shared table would make the refusal not happen.
    """

    code = models.CharField(max_length=20)


class TargetedSession(models.Model):
    """A projected table reserved for the assertion that a projection takes a target too.

    A collection copied along a join carries the source's skew into a second
    table, and the route the rows took in has nothing to do with whether the
    planner can record it.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class Period(models.Model):
    """A subscription's validity chain, whose current row has no end.

    Here because ``None`` is a legitimate value for the special row of a group,
    which is what makes "not passed" impossible to spell as ``None`` in
    ``PerParent``. An SCD-2 chain says exactly that: the current period is the
    one whose ``valid_to`` is unset.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True)


class Seat(models.Model):
    """A model whose two-column uniqueness is decidable arithmetic.

    ``Table`` declines multi-column uniqueness on purpose -- it cannot know how
    many companies exist -- so this is the case that only a whole shape can
    refuse: rows against the product of the parent count and the label count.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    label = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "label"], name="one_seat_label_per_company"),
        ]


class Booking(models.Model):
    """A model carrying every conditional constraint the pre-check cannot read.

    One model rather than seven, because what is being tested is a set of
    skips: a condition that is not a single equality, one written over an
    expression rather than fields, one grouped by a column no fan-out
    partitions, one joining two clauses, one whose single clause is itself a
    nested Q, one comparing rather than equating, and a check constraint, which
    is not a unique constraint at all. A declaration naming this model has to be
    accepted whole.

    ``seats`` is what makes the comparison bite: declared as ``Constant(0)`` a
    condition read as ``seats == 0`` would be refused, so a suffix check that
    stopped distinguishing a lookup from an equality would show here rather than
    passing quietly.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    room = models.CharField(max_length=20)
    state = models.CharField(max_length=20)
    seats = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state__in=["HELD", "PAID"]),
                name="booking_state_in_is_not_an_equality",
            ),
            models.UniqueConstraint(
                models.functions.Lower("room"),
                condition=models.Q(state="HELD"),
                name="booking_over_an_expression",
            ),
            models.UniqueConstraint(
                fields=["room"],
                condition=models.Q(state="HELD"),
                name="booking_grouped_by_a_plain_column",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="HELD") & models.Q(seats__gt=0),
                name="booking_over_two_clauses",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(models.Q(state="HELD") | models.Q(state="PAID")),
                name="booking_over_a_nested_clause",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(seats__gt=0),
                name="booking_over_a_comparison",
            ),
            models.CheckConstraint(
                name="booking_seats_are_not_negative",
                # Django renamed CheckConstraint's predicate from ``check`` to
                # ``condition`` in 5.1 and removed the old name in 6.0, and this
                # package's floor is 4.2 -- so there is no single spelling, and
                # the alternative to this is not declaring a check constraint at
                # all, which would leave the "not a unique constraint" branch
                # covered by a stub rather than by a model.
                **(
                    {"condition": models.Q(seats__gte=0)}
                    if django.VERSION >= (5, 1)
                    else {"check": models.Q(seats__gte=0)}
                ),
            ),
        ]


class Invitation(models.Model):
    """A conditional constraint over a column this package may leave undeclared.

    ``outcome`` is nullable, so a shape can legitimately say nothing about it --
    and a constraint conditioned on a column nobody fills is a constraint with
    nothing to weigh.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    label = models.CharField(max_length=20)
    outcome = models.CharField(max_length=20, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(outcome="ACCEPTED"),
                name="one_accepted_invitation_per_company",
            ),
        ]


class Contest(models.Model):
    """A parent whose children have several winners rather than one.

    No constraint at all, which is the point: ``count=`` is for the rule a
    schema does not state, and a unique constraint is the case it is not.
    """

    name = models.CharField(max_length=50)


class Entry(models.Model):
    """A contest's entries, a fixed number of which win."""

    contest = models.ForeignKey(Contest, on_delete=models.CASCADE)
    placing = models.CharField(max_length=20)


class Assignment(models.Model):
    """A child with two parents, only one of which its constraint groups by.

    The model that makes "grouped by the wrong thing" reachable: a rule kept
    once per contest says nothing about how many leads a company ends up with,
    and with a single foreign key the table's own check would refuse first for
    an unrelated reason.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    contest = models.ForeignKey(Contest, on_delete=models.CASCADE)
    role = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(role="LEAD"),
                name="one_lead_assignment_per_company",
            ),
        ]


class Document(models.Model):
    """The parent half of a multi-table inheritance pair.

    Its own table, holding ``title``. That column is what makes the pair worth
    having: it appears in the child's ``_meta.concrete_fields`` and in no column
    of the child's table, which is the disagreement the inheritance refusal is
    about.
    """

    title = models.CharField(max_length=100)


class DeliveryDocument(Document):
    """The child half, whose rows live in two tables at once.

    Concrete inheritance rather than abstract, so Django gives it a table of its
    own holding ``document_ptr_id`` and ``tracking`` while ``title`` stays next
    door.
    """

    tracking = models.CharField(max_length=50)


class CompanyProxy(Company):
    """A proxy, which shares its parent's table rather than adding one.

    Here because ``_meta.parents`` is non-empty for a proxy too, so the
    inheritance refusal has to tell the two apart: a proxy declares no column
    and no table of its own, and a shape naming it is a shape about the table it
    proxies.
    """

    class Meta:
        proxy = True


class Person(models.Model):
    """One end of a many-to-many edge."""

    name = models.CharField(max_length=50)


class Membership(models.Model):
    """A through table whose uniqueness two fan-outs cannot keep.

    The pair is unique, and each foreign key is partitioned over its own parents
    independently of the other, so which pairs come out together is an artefact
    of the row index. It fits -- the product of the two parent counts is far
    larger than the row count -- which is why the pigeonhole arithmetic passes
    it and something else has to refuse it.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    role = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "person"], name="one_membership_per_company_person"
            ),
        ]


class Timestamped(models.Model):
    """An abstract base, whose column belongs to whoever inherits it.

    The inheritance that is not multi-table: Django copies the field onto the
    child, so it writes to the child's own table and nothing lands next door.
    """

    created_at = models.DateTimeField()

    class Meta:
        abstract = True


class Memo(Timestamped):
    """A model inheriting abstractly, which is an ordinary single-table model.

    Here so that the inheritance refusal has to distinguish the two kinds. Every
    column it has is a column of its own table, so a shape naming it is a shape
    about one table and nothing about it is refusable.
    """

    body = models.CharField(max_length=100)
