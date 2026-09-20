"""Binds each browser check to the scenario it is evidence for.

The identifier has to survive into the reported test name, not just into a
docstring, so that a run's output can be quoted as the record of what was
observed. A single-value parametrisation is the plainest way to put an
arbitrary string there: the node id ends in ``[@scenario:<id>]``, which
``pytest -k`` can also select on.
"""

import pytest


def scenario(scenario_id: str) -> pytest.MarkDecorator:
    """Tag a check with its scenario id and hand the id to the test."""
    return pytest.mark.parametrize("scenario_id", [scenario_id], ids=[f"@scenario:{scenario_id}"])
