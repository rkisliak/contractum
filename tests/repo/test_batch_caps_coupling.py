"""Contracts' impact page limits follow validation's batch caps (IN-06).

Contracts cannot read validation's settings, so it refuses ``DC_IMPACT_PAGE_*`` above a copy of
validation's defaults (``VALIDATION_BATCH_CAPS``). This test fails when either side changes a
default without the other, instead of every impact page becoming a 422 there and a 503 here.
"""

from __future__ import annotations

import pytest
from dc_contracts.settings import VALIDATION_BATCH_CAPS
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit


def test_contracts_copy_of_the_batch_caps_matches_validations_defaults() -> None:
    fields = ValidationSettings.model_fields
    assert VALIDATION_BATCH_CAPS == {
        "impact_page_size": fields["max_batch_items"].default,
        "impact_page_max_folders": fields["max_batch_folders"].default,
        "impact_page_max_bytes": fields["max_batch_bytes"].default,
    }
