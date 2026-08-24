import pytest

from w2c_render.source_policy import SourcePolicy, source_policy_from_values


def test_default_policy_preserves_the_documented_no_import_contract():
    policy = SourcePolicy()
    assert policy.violation("export default function Widget(){ return <div/> }") is None
    assert "not allowed" in policy.violation(
        "import { LuSearch } from 'react-icons/lu';\nexport default function Widget(){}"
    )


def test_package_subpath_allowlist_is_exact_and_does_not_allow_local_files():
    policy = source_policy_from_values(["react-icons/*"])
    assert policy.violation(
        "import { LuSearch } from 'react-icons/lu';\nexport default function Widget(){}"
    ) is None
    assert "not allowed" in policy.violation("import React from 'react';")
    assert "local or absolute" in policy.violation("import helper from './helper.js';")
    assert "non-package" in policy.violation("import helper from 'https://example.com/x.js';")


def test_an_imported_binding_named_from_does_not_hide_the_module_specifier():
    source = "import { from as x } from 'blocked'; export default function Widget(){}"
    assert "not allowed" in SourcePolicy().violation(source)
    assert SourcePolicy(("blocked",)).violation(source) is None


def test_a_reexported_binding_named_from_does_not_hide_the_module_specifier():
    source = "export { from as x } from 'blocked'; export default function Widget(){}"
    assert "not allowed" in SourcePolicy().violation(source)


def test_comments_and_displayed_strings_do_not_look_like_imports():
    source = """// import x from 'blocked';
export default function Widget() {
  return <div>{"import('blocked')"}</div>;
}
"""
    assert SourcePolicy().violation(source) is None


def test_dynamic_import_requires_both_a_flag_and_an_allowed_literal():
    source = "const x = import('react-icons/lu'); export default function Widget(){}"
    assert "dynamic import" in SourcePolicy(("react-icons/*",)).violation(source)
    assert SourcePolicy(("react-icons/*",), True).violation(source) is None
    assert "literal" in SourcePolicy(("react-icons/*",), True).violation(
        "const x = import(name); export default function Widget(){}"
    )


@pytest.mark.parametrize(
    "pattern",
    ["../x", "/x", "react-*icons", "*", "https://example.com/x.js", "pkg/../x", ""],
)
def test_invalid_allowlist_patterns_fail_closed(pattern):
    with pytest.raises(ValueError):
        SourcePolicy((pattern,))


def test_policy_id_changes_only_with_effective_capabilities():
    first = source_policy_from_values(["react-icons/*", "react"])
    reordered = source_policy_from_values(["react", "react-icons/*"])
    dynamic = source_policy_from_values(
        ["react", "react-icons/*"], allow_dynamic_imports=True
    )
    assert first.policy_id == reordered.policy_id
    assert first.policy_id != dynamic.policy_id
    assert first.descriptor()["allowed_imports"] == ["react", "react-icons/*"]
