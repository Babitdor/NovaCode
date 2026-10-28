"""Google tool declarations must describe HTTP authentication arrays."""

from __future__ import annotations

import pytest
from langchain_google_genai._function_utils import convert_to_genai_function_declarations
from pydantic import ValidationError

from novacode_cli.tools.fetch_tools import fetch_url


def test_fetch_auth_has_items_in_google_declaration():
    declaration = convert_to_genai_function_declarations([fetch_url])[0]
    auth = declaration.function_declarations[0].parameters.properties["auth"]
    assert auth.items is not None
    assert auth.items.type.value == "STRING"


def test_fetch_auth_preserves_basic_auth_tuple():
    parsed = fetch_url.args_schema.model_validate(
        {"url": "https://example.com", "auth": ["user", "password"]}
    )
    assert parsed.auth == ("user", "password")
    assert isinstance(parsed.auth, tuple)


@pytest.mark.parametrize("auth", [[], ["user"], ["user", "password", "extra"]])
def test_fetch_auth_rejects_wrong_number_of_credentials(auth: list[str]):
    with pytest.raises(ValidationError):
        fetch_url.args_schema.model_validate({"url": "https://example.com", "auth": auth})
