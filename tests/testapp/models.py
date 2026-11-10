from __future__ import annotations

import uuid
from typing import Any

import django
from django.db import models
from django.db.models import lookups  # noqa: F401  (reached as models.lookups below)


class Company(models.Model):
    """A plain table with nothing optional about it."""

    name = models.CharField(max_length=200)


class Order(models.Model):
    """One table covering every kind of column the validator reasons about."""

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
    """A fan-out child carrying a per-group invariant."""

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
    """A model whose column name collides with Table's own signature."""

    rows = models.IntegerField()


class Defaulted(models.Model):
    """A model whose default is a callable, which cannot be guessed at."""

    token = models.UUIDField(default=uuid.uuid4)


class Prepared(models.Model):
    """A model for checking that field preparation actually happens."""

    at = models.DateTimeField()
    tags = models.JSONField()


class SlugPk(models.Model):
    """A model whose primary key this package cannot assign."""

    code = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=50)


class ShortCode(models.Model):
    """A character primary key too short for a UUID's 36 characters."""

    code = models.CharField(max_length=8, primary_key=True)
    name = models.CharField(max_length=50)


class Referred(models.Model):
    """A model with an optional self-relation, which may be left undeclared."""

    label = models.CharField(max_length=50)
    referrer = models.ForeignKey("self", null=True, on_delete=models.SET_NULL)


class Subscriber(models.Model):
    """A model with a single-column unique constraint."""

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
    """A child of a UUID-keyed parent."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="records")
    label = models.CharField(max_length=50)


class Reading(models.Model):
    """A model keyed by a timestamp, which needs its key prepared like any value."""

    at = models.DateTimeField(primary_key=True)
    value = models.IntegerField()


class Catalogue(models.Model):
    """A table nothing else in the suite builds."""

    name = models.CharField(max_length=50)


class Account(models.Model):
    """A parent whose own columns a child derives from."""

    signed_up_at = models.DateTimeField()
    plan = models.CharField(max_length=20)


class Ticket(models.Model):
    """A child carrying all four faces of the derivation mechanism at once."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="tickets")
    opened_at = models.DateTimeField()
    severity = models.CharField(max_length=20)
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)


class Playhouse(models.Model):
    """A parent whose date column is a ``DateField``, not a ``DateTimeField``."""

    opened_on = models.DateField()
    announced_at = models.DateTimeField()


class Performance(models.Model):
    """A child with one column of each temporal kind, so both directions test."""

    playhouse = models.ForeignKey(Playhouse, on_delete=models.CASCADE, related_name="performances")
    starts_at = models.DateTimeField()
    doors_on = models.DateField()


class ActiveVendorManager(models.Manager):
    """A default manager that hides rows, which is an ordinary thing to write."""

    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(retired=False)


class Vendor(models.Model):
    """A parent whose default manager hides some of its own rows."""

    name = models.CharField(max_length=50)
    retired = models.BooleanField(default=False)

    objects = ActiveVendorManager()


class Supply(models.Model):
    """A child of a partly-hidden parent, carrying one of its columns."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    vendor_name = models.CharField(max_length=50)


class Template(models.Model):
    """The thing an event is created from, and the collection's real owner."""

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
    """One of each kind of column a derived projection has to decide about."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="sessions")
    source = models.ForeignKey(TemplateSession, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=50)
    minutes = models.IntegerField()
    channel = models.CharField(max_length=20, default="web")
    note = models.TextField(null=True)


class Attendance(models.Model):
    """A fan-out child of a projected table."""

    session = models.ForeignKey(EventSession, on_delete=models.CASCADE)
    name = models.CharField(max_length=50)


class Rehearsal(models.Model):
    """A collection joinable to an event through two different models."""

    template = models.ForeignKey(Template, on_delete=models.CASCADE)
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class SparseSession(models.Model):
    """A projected model with a column nothing can fill."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)
    headcount = models.IntegerField()


class TokenSession(models.Model):
    """A projected model with a callable default."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)
    token = models.UUIDField(default=uuid.uuid4)


class KeyedSession(models.Model):
    """A UUID-keyed projection target whose table name hashes above 2^63."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=50)
    minutes = models.IntegerField()


class UuidSession(models.Model):
    """A projected model whose keys cannot be written in SQL."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class SessionNote(models.Model):
    """A UUID-keyed note on a UUID-keyed session, so two Disjoint tables deep."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(UuidSession, null=True, on_delete=models.CASCADE)
    text = models.CharField(max_length=50)


class DualSession(models.Model):
    """A projected model with two edges into the table it is projected per."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    replaces = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=50)


class AuditedSession(models.Model):
    """A projected table reserved for the statistics assertion."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class Bucketed(models.Model):
    """A table reserved for the assertion that a declared statistics target lands."""

    code = models.CharField(max_length=20)


class Narrowed(models.Model):
    """A table reserved for the refusal that a target is what avoids."""

    code = models.CharField(max_length=20)


class TargetedSession(models.Model):
    """A projected table reserved for the assertion that a projection takes a target too."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)


class Period(models.Model):
    """A subscription's validity chain, whose current row has no end."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True)


class Seat(models.Model):
    """A model whose two-column uniqueness is decidable arithmetic."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    label = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "label"], name="one_seat_label_per_company"),
        ]


class Booking(models.Model):
    """A model carrying every conditional constraint the pre-check cannot read."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    room = models.CharField(max_length=20)
    state = models.CharField(max_length=20)
    seats = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("room"),
                condition=models.Q(state="HELD"),
                name="booking_over_an_expression",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="HELD") & models.Q(seats__gt=0),
                name="booking_over_two_clauses",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="HELD") | models.Q(room="one"),
                name="booking_over_two_columns_in_one_or",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=~(models.Q(state="HELD") | models.Q(state="PAID")),
                name="booking_over_a_negated_or",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(state="HELD") | models.Q(seats__gt=0),
                name="booking_over_an_or_with_a_comparison",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(models.lookups.LessThan(models.F("seats"), models.Value(10))),
                name="booking_over_a_bare_lookup_expression",
            ),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(seats__gt=0),
                name="booking_over_a_comparison",
            ),
            models.CheckConstraint(
                name="booking_seats_are_not_negative",
                # No CheckConstraint spelling spans Django 4.2 to 6.0, so this is a real model.
                **(
                    {"condition": models.Q(seats__gte=0)}
                    if django.VERSION >= (5, 1)
                    else {"check": models.Q(seats__gte=0)}
                ),
            ),
        ]


class Invitation(models.Model):
    """A conditional constraint over a column this package may leave undeclared."""

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
    """A parent whose children have several winners rather than one."""

    name = models.CharField(max_length=50)


class Entry(models.Model):
    """A contest's entries, a fixed number of which win."""

    contest = models.ForeignKey(Contest, on_delete=models.CASCADE)
    placing = models.CharField(max_length=20)


class Assignment(models.Model):
    """A child with two parents, only one of which its constraint groups by."""

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
    """The parent half of a multi-table inheritance pair."""

    title = models.CharField(max_length=100)


class DeliveryDocument(Document):
    """The child half, whose rows live in two tables at once."""

    tracking = models.CharField(max_length=50)


class CompanyProxy(Company):
    """A proxy, which shares its parent's table rather than adding one."""

    class Meta:
        proxy = True


class Person(models.Model):
    """One end of a many-to-many edge."""

    name = models.CharField(max_length=50)


class Membership(models.Model):
    """A through table whose uniqueness two fan-outs cannot keep."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    role = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "person"], name="one_membership_per_company_person"
            ),
        ]


class Engagement(models.Model):
    """``Membership`` spelled the older way, with ``Meta.unique_together``."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="+")

    class Meta:
        unique_together = [("company", "person")]


class TenantGrant(models.Model):
    """An edge table whose partner is keyed by UUID."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=20)


class Timestamped(models.Model):
    """An abstract base, whose column belongs to whoever inherits it."""

    created_at = models.DateTimeField()

    class Meta:
        abstract = True


class Memo(Timestamped):
    """A model inheriting abstractly, which is an ordinary single-table model."""

    body = models.CharField(max_length=100)


class Review(models.Model):
    """A partial unique whose condition names a **set** of values, not one."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, default="DRAFT")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(status__in=["DRAFT", "IN_REVIEW", "APPROVED"]),
                name="one_open_review_per_company",
            ),
        ]


def get_default_company() -> int:
    """A callable default that would read the database if anything called it."""
    return Company.objects.values_list("pk", flat=True).first() or 1


class Delegated(models.Model):
    """A required foreign key whose default is a **callable**."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, default=get_default_company)
    label = models.CharField(max_length=20)


class Assigned(models.Model):
    """A required foreign key carrying a Python-level ``default=``."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, default=1)
    label = models.CharField(max_length=20)


class Escalation(models.Model):
    """A set condition written as a ``set``, which has no order of its own."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, default="RAISED")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(status__in={"RAISED", "ACKNOWLEDGED"}),
                name="one_live_escalation_per_company",
            ),
        ]


class Coupon(models.Model):
    """A composite uniqueness with **no relation in it at all**."""

    batch = models.CharField(max_length=20)
    code = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "code"], name="one_code_per_batch"),
        ]


class Voucher(models.Model):
    """A composite uniqueness one of whose columns may be left undeclared."""

    batch = models.CharField(max_length=20)
    code = models.CharField(max_length=20, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "code"], name="one_voucher_code_per_batch"),
        ]


class Ticketed(models.Model):
    """A unique column a derivation fills, beside a one-to-one relation."""

    reference = models.CharField(max_length=100, unique=True)
    prefix = models.CharField(max_length=10)
    company = models.OneToOneField(Company, null=True, on_delete=models.SET_NULL)


class Submission(models.Model):
    """A partial unique whose condition is an **OR-tree of equalities**."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, default="DRAFT")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=(
                    models.Q(status="DRAFT")
                    | models.Q(status="IN_REVIEW")
                    | models.Q(status="APPROVED")
                ),
                name="one_open_submission_per_company",
            ),
        ]


class Approval(models.Model):
    """An OR-tree that is nested **and** names one of its values twice."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, default="OPEN")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(
                    models.Q(status="OPEN") | models.Q(status="OPEN") | models.Q(status="HELD")
                ),
                name="one_open_approval_per_company",
            ),
        ]


class Auditor(models.Model):
    """Stands in for the ``User`` an audit base points every model at."""

    name = models.CharField(max_length=50)


class Plan(models.Model):
    """The legitimate shared parent: the model a projection's join runs through."""

    name = models.CharField(max_length=50)


class Portfolio(models.Model):
    """A second legitimate shared parent, so ``through=`` has a real choice."""

    name = models.CharField(max_length=50)


class PlanStage(models.Model):
    """The collection being copied, carrying audit columns like everything else."""

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="stages")
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="stages")
    title = models.CharField(max_length=50)
    created_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")


class PlanRun(models.Model):
    """What the projected rows hang off, carrying the same audit columns."""

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="runs")
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="runs")
    name = models.CharField(max_length=50)
    created_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")


class RunStage(models.Model):
    """A projection whose derived join an audit base makes ambiguous."""

    run = models.ForeignKey(PlanRun, on_delete=models.CASCADE, related_name="stages")
    source = models.ForeignKey(PlanStage, null=True, on_delete=models.SET_NULL, related_name="+")
    title = models.CharField(max_length=50)
    created_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(Auditor, null=True, on_delete=models.SET_NULL, related_name="+")


class Region(models.Model):
    """A UUID-keyed root, which is what a shared abstract base usually produces."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)


class Depot(models.Model):
    """The ``per`` side of a projection over UUID keys."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="depots")
    name = models.CharField(max_length=50)


class Route(models.Model):
    """The ``copying`` side, joinable to ``Depot`` through ``Region``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="routes")
    label = models.CharField(max_length=50)


class Shipment(models.Model):
    """A projected table whose two measure columns are related to each other."""

    depot = models.ForeignKey(Depot, on_delete=models.CASCADE, related_name="shipments")
    source = models.ForeignKey(Route, null=True, on_delete=models.SET_NULL, related_name="+")
    label = models.CharField(max_length=50)
    requested_amount = models.IntegerField()
    approved_amount = models.IntegerField()
    settled_amount = models.IntegerField()


class Club(models.Model):
    """A parent that a chain of foreign keys leads back to from its own child."""

    name = models.CharField(max_length=50)
    selected_fee = models.ForeignKey(
        "MemberFee", null=True, on_delete=models.SET_NULL, related_name="+"
    )


class Section(models.Model):
    """The declared child in a scaled world over a caller's clubs."""

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=50)


class MemberFee(models.Model):
    """An undeclared table holding a foreign key into the declared one."""

    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="fees")
    amount = models.IntegerField()


class Badge(models.Model):
    """The end of a many-to-many relation that does not declare it."""

    name = models.CharField(max_length=50)


class Wearer(models.Model):
    """The end that declares both relations to ``Badge``."""

    name = models.CharField(max_length=50)
    badges = models.ManyToManyField(Badge, related_name="wearers")
    awards = models.ManyToManyField(Badge, through="Award", related_name="awarded_to")


class Award(models.Model):
    """The through model ``Wearer.awards`` declares rather than leaving to Django."""

    wearer = models.ForeignKey(Wearer, on_delete=models.CASCADE, related_name="+")
    badge = models.ForeignKey(Badge, on_delete=models.CASCADE, related_name="+")


class Thread(models.Model):
    """A UUID-keyed table with a key into itself and one the database never sees."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey(
        Company, null=True, db_constraint=False, on_delete=models.DO_NOTHING, related_name="+"
    )
    title = models.CharField(max_length=50)


class TenantProfile(models.Model):
    """A primary key that is itself a one-to-one key into a UUID-keyed table."""

    tenant = models.OneToOneField(
        Tenant, primary_key=True, on_delete=models.CASCADE, related_name="+"
    )
    text = models.CharField(max_length=50)


class StrictCodeField(models.CharField):
    """A character field whose ``to_python`` refuses with a plain exception."""

    refusal: type[Exception] = ValueError

    def to_python(self, value: object) -> object:
        text = super().to_python(value)
        if text is not None and not str(text).startswith("code-"):
            raise self.refusal(f"{text!r} is not a code")
        return text


class StrictCode(models.Model):
    """A primary key on a ``StrictCodeField``, which refuses a UUID's text form."""

    code = StrictCodeField(max_length=50, primary_key=True)
    name = models.CharField(max_length=50)


class Tariff(models.Model):
    """A primary key on a ``DecimalField``, whose reading of a float is not the float."""

    rate = models.DecimalField(max_digits=10, decimal_places=2, primary_key=True)


class Remark(models.Model):
    """A row that references one of the three tables above, and none of them is declared."""

    thread = models.ForeignKey(Thread, null=True, on_delete=models.CASCADE, related_name="+")
    profile = models.ForeignKey(
        TenantProfile, null=True, on_delete=models.CASCADE, related_name="+"
    )
    code = models.ForeignKey(StrictCode, null=True, on_delete=models.CASCADE, related_name="+")
    text = models.CharField(max_length=50)


class CasedTable(models.Model):
    """A table whose name holds capitals, so it is found only where it is quoted."""

    code = models.CharField(max_length=20)

    class Meta:
        db_table = "testapp_CasedTable"


class DottedTable(models.Model):
    """A table whose name holds a dot, which Django quotes as part of one identifier."""

    code = models.CharField(max_length=20)

    class Meta:
        db_table = "testapp.dotted_table"


class Category(models.Model):
    """A tree: a nullable foreign key to its own table, the column ``Tree`` fills."""

    name = models.CharField(max_length=50)
    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="children")


class RootlessNode(models.Model):
    """A self-reference that cannot be null, which no tree can fill."""

    parent = models.ForeignKey("self", on_delete=models.CASCADE)


class Successor(models.Model):
    """A unique self-reference: at most one row may point at each."""

    previous = models.OneToOneField(
        "self", null=True, on_delete=models.SET_NULL, related_name="following"
    )


class Folder(models.Model):
    """A tree whose names are unique among siblings, as a file system's are."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="one_name_per_folder"),
        ]


class Chain(models.Model):
    """A self-reference unique by constraint rather than by ``unique=True``."""

    previous = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["previous"], name="one_link_after_each"),
        ]


class Relay(models.Model):
    """A self-reference unique through ``Meta.unique_together`` on the column alone."""

    previous = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        unique_together = [("previous",)]


class RootlessLink(models.Model):
    """A unique self-reference that refuses NULL: no tree has a first row to give it."""

    previous = models.OneToOneField("self", on_delete=models.CASCADE, related_name="+")


class TiedLink(models.Model):
    """A unique self-reference whose NULLs count as one value, where Django can say so."""

    previous = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["previous"],
                name="one_tied_link_after_each",
                **({"nulls_distinct": False} if django.VERSION >= (5, 0) else {}),
            ),
        ]


class SpelledChain(models.Model):
    """A unique self-reference whose constraint spells the attname, ``previous_id``."""

    previous = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["previous_id"], name="one_spelled_link_after_each"),
        ]


class SpelledRelay(models.Model):
    """The same, through ``unique_together``, which takes either spelling too."""

    previous = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        unique_together = [("previous_id",)]


class SpelledFolder(models.Model):
    """``Folder``'s uniqueness over ``(parent, name)``, spelled ``parent_id``."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent_id", "name"], name="one_name_per_spelled_folder"
            ),
        ]


class Mirror(models.Model):
    """A self-key unique together with a key to another table."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "company"], name="one_company_per_mirror"),
        ]


class NamedMirror(models.Model):
    """``Mirror`` with a plain column in the uniqueness beside the key to another table."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "company", "name"], name="one_named_company_per_mirror"
            ),
        ]


class OnlyChild(models.Model):
    """A one-to-one self-key unique together with a name: the key alone keeps it."""

    parent = models.OneToOneField("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="one_name_per_only_child"),
        ]


class TiedOnlyChild(models.Model):
    """A one-to-one self-key unique together with a name, where its NULLs count as one value."""

    parent = models.OneToOneField("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="one_name_per_tied_only_child",
                **({"nulls_distinct": False} if django.VERSION >= (5, 0) else {}),
            ),
        ]


class Coded(models.Model):
    """A self-key beside a uniqueness that does not include it, by constraint and by tuple."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        unique_together = [("name", "code")]
        constraints = [
            models.UniqueConstraint(fields=["code", "name"], name="one_code_per_name"),
        ]


class Shelf(models.Model):
    """``Folder``'s uniqueness through ``unique_together``, the other place Django keeps one."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        unique_together = [("parent", "name")]


class SpelledShelf(models.Model):
    """``Shelf`` with the key spelled by its attname, which ``unique_together`` keeps as written."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50)

    class Meta:
        unique_together = [("parent_id", "name")]


class RootlessShelf(models.Model):
    """``Shelf`` with a key that refuses NULL, so no tree can fill it and none is written."""

    parent = models.ForeignKey("self", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        unique_together = [("parent", "name")]


class Sibling(models.Model):
    """A self-reference whose only uniqueness is conditional, so the column is not unique."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent"],
                condition=models.Q(name="only"),
                name="one_only_child",
            ),
        ]


class UnenforcedConstraint(models.constraints.BaseConstraint):
    """A constraint that is not a ``UniqueConstraint`` and has no ``condition`` to speak of."""

    condition = None

    def constraint_sql(self, model: Any, schema_editor: Any) -> None:
        return None

    def create_sql(self, model: Any, schema_editor: Any) -> None:
        return None

    def remove_sql(self, model: Any, schema_editor: Any) -> None:
        return None

    def validate(
        self, model: Any, instance: Any, exclude: Any = None, using: str = "default"
    ) -> None:
        return None


class Exclusive(models.Model):
    """A self-reference under a constraint that says nothing about uniqueness."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [UnenforcedConstraint(name="exclusive_says_nothing")]


class Checked(models.Model):
    """A self-reference with a check constraint, which says nothing about uniqueness."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="checked_is_not_its_own_parent",
                # See Booking: the predicate's keyword is not one spelling across the floor.
                **(
                    {"condition": ~models.Q(parent=models.F("id"))}
                    if django.VERSION >= (5, 1)
                    else {"check": ~models.Q(parent=models.F("id"))}
                ),
            ),
        ]


class Limb(models.Model):
    """A tree beside a fan-out under one uniqueness, as a real table."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent", "company"], name="one_limb_per_pair"),
        ]


class Spur(models.Model):
    """``Limb`` with a third column, which is what can keep the triple apart."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")
    label = models.CharField(max_length=50, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "company", "label"], name="one_spur_per_triple"
            ),
        ]


class Rung(models.Model):
    """Two self-keys under one uniqueness."""

    left = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    right = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["left", "right"], name="one_rung_per_pair"),
        ]


class LabelledRung(models.Model):
    """Two self-keys and a third column."""

    left = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    right = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    label = models.CharField(max_length=50, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["left", "right", "label"], name="one_rung_per_triple"),
        ]


class Verdict(models.Model):
    """A conditional uniqueness over a tree and a second column."""

    parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=50, null=True)
    status = models.CharField(max_length=50, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "code"],
                condition=models.Q(status="accepted"),
                name="one_accepted_verdict",
            ),
        ]
