"""The draw derivation everything else rests on."""

from __future__ import annotations

from django_data_shape.utils import draw, field_stream


def test_a_draw_is_uniform_in_the_unit_interval() -> None:
    stream = field_stream(seed=7, table="orders", field="status")
    values = [draw(stream, row) for row in range(2000)]

    assert all(0.0 <= value < 1.0 for value in values)
    # Not a distribution test, a smoke test: a mixer that collapsed would show
    # up here long before it showed up as a wrong-looking database.
    assert len(set(values)) > 1900


def test_a_draw_depends_only_on_its_stream_and_row() -> None:
    stream = field_stream(seed=7, table="orders", field="status")

    # The property the placement work depends on: asking for row 900 first does
    # not change what row 900 is. A generator carrying sequential RNG state
    # would fail this, and would then be unable to emit rows out of order.
    forwards = [draw(stream, row) for row in range(1000)]
    backwards = [draw(stream, row) for row in reversed(range(1000))]

    assert forwards == list(reversed(backwards))


def test_fields_and_seeds_produce_independent_streams() -> None:
    status = field_stream(seed=7, table="orders", field="status")
    total = field_stream(seed=7, table="orders", field="total")
    other_table = field_stream(seed=7, table="projects", field="status")
    other_seed = field_stream(seed=8, table="orders", field="status")

    assert len({status, total, other_table, other_seed}) == 4
    assert draw(status, 0) != draw(total, 0)


def test_a_stream_is_stable_across_processes() -> None:
    # Pinned, not recomputed. ``hash()`` is salted per interpreter run, so a
    # shape seeded today would reproduce only within one process if the stream
    # derivation ever regressed to it -- and the failure would look like flaky
    # test data rather than like a bug here.
    assert field_stream(seed=0, table="orders", field="status") == 10965613546237361956
    assert draw(field_stream(seed=0, table="orders", field="status"), 0) == 0.9705692634847263
