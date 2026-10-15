"""Input validation checks for public business registration."""
import pytest
from pydantic import ValidationError
from routers.auth import RegisterBody


def registration(**overrides):
    fields = {
        "email": "person@example.com",
        "password": "a-valid-long-password",
        "name": "Business Owner",
        "shop_name": "New Business",
    }
    fields.update(overrides)
    return RegisterBody(**fields)


@pytest.mark.parametrize("password", ["", "short", "x" * 73, "é" * 37])
def test_rejects_unsafe_password_sizes(password):
    with pytest.raises(ValidationError):
        registration(password=password)


def test_accepts_password_at_minimum_length():
    assert registration(password="a" * 12).password == "a" * 12


@pytest.mark.parametrize("field", ["name", "shop_name"])
@pytest.mark.parametrize("value", ["", "   ", "x" * 121])
def test_rejects_empty_or_oversized_names(field, value):
    with pytest.raises(ValidationError):
        registration(**{field: value})


def test_normalizes_names():
    user = registration(name="  Business Owner  ", shop_name="  New Business  ")
    assert user.name == "Business Owner"
    assert user.shop_name == "New Business"
