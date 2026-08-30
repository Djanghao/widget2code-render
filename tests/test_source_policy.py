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


# ---- the two named contracts ------------------------------------------------

from w2c_render.source_policy import MODES, policy_for_mode  # noqa: E402


def test_m1_forbids_every_import_and_provides_the_globals_instead():
    m1 = policy_for_mode("m1")
    assert m1.globals == ("React", "Recharts")
    assert m1.violation("import { AreaChart } from 'recharts';") is not None
    assert m1.violation("import { PiEyeBold } from 'react-icons/pi';") is not None
    assert m1.violation("export default function Widget(){ return <Recharts.PieChart/>; }") is None


def test_m2_allows_three_packages_and_provides_no_globals():
    """React is not among them on purpose: the automatic JSX runtime makes importing it
    unnecessary, and its absence is what puts hooks and state out of reach rather than
    merely discouraging them."""
    m2 = policy_for_mode("m2")
    assert m2.globals == ()
    for allowed in ("recharts", "react-icons/pi", "react-icons/si"):
        assert m2.violation(f"import x from '{allowed}';") is None, allowed
    for refused in ("react", "react-dom", "react-icons/fa", "echarts", "lodash"):
        assert m2.violation(f"import x from '{refused}';") is not None, refused


def test_the_two_contracts_are_exclusive_rather_than_nested():
    """Written for the wrong one, a widget fails rather than quietly working: m1 source
    under m2 has no globals to reach, m2 source under m1 has its imports refused."""
    m1, m2 = policy_for_mode("m1"), policy_for_mode("m2")
    assert set(m1.allowed_imports) & set(m2.allowed_imports) == set()
    assert set(m1.globals) & set(m2.globals) == set()
    assert m1.policy_id != m2.policy_id


def test_a_mode_names_itself_in_what_every_render_carries():
    """The descriptor rides on every result, so a collection can be checked for the
    contract it was produced under instead of trusted to have used one."""
    for name in ("m1", "m2"):
        descriptor = policy_for_mode(name).descriptor()
        assert descriptor["mode"] == name
        assert descriptor["policy_id"].startswith("source_policy_")
        assert set(descriptor) == {
            "policy_id", "schema_version", "mode", "allowed_imports",
            "allow_dynamic_imports", "globals",
        }


def test_an_unknown_mode_names_the_ones_that_exist():
    import pytest

    with pytest.raises(ValueError) as caught:
        policy_for_mode("m3")
    assert "m1" in str(caught.value) and "m2" in str(caught.value)
    assert set(MODES) == {"m1", "m2"}
