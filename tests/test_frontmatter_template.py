from obsidian_mcp import frontmatter_template as template


def test_type_field_keys_are_all_valid_types():
    """TYPE_FIELD's keys must be a subset of TYPE_FOLDER's - a typo'd type name
    here would silently require an extra field on a type that doesn't exist."""
    assert set(template.TYPE_FIELD) <= set(template.TYPE_FOLDER)


def test_version_is_a_non_empty_string():
    assert isinstance(template.VERSION, str)
    assert template.VERSION
