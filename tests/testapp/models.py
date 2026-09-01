"""Models the shape vocabulary is exercised against.

Deliberately a small graph rather than one table: a shape declaration only earns
its keep over an ordinary loop once there are edges, so the suite's own fixture
schema has to have them.
"""

from __future__ import annotations

from django.db import models


class Company(models.Model):
    """A fan-out parent."""

    name = models.CharField(max_length=200)


class Project(models.Model):
    """A fan-out child carrying a per-group invariant.

    At most one project per company may be ``ACTIVE``. That constraint is the
    worked example for the whole invariant layer: it is what makes the marginal
    distribution of ``status`` a *derived* quantity rather than a declarable one.
    """

    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"

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
