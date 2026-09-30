"""The Portfolio column picker: hide and reorder columns, saved per login.

Asked for as "make every column except the Sales, ad spend, units and weights… hiddenable", then
"also make the columns slidable — which one first, which later, or last". See
docs/superpowers/specs/2026-09-30-portfolio-column-picker-design.md.
"""
import pytest

pytestmark = pytest.mark.regression


def test_users_has_a_nullable_preferences_column():
    """NULL means "never chosen", deliberately distinct from "chose the default": it is what lets a
    later change of default reach everyone who never customised."""
    from app.models import User

    column = User.__table__.c.preferences_json
    assert column.nullable is True
    assert column.server_default is None
