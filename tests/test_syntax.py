from pathlib import Path

from w2c_render.syntax import check_syntax, format_syntax_error


def test_valid_jsx_passes_syntax_check(tmp_path: Path):
    source = tmp_path / "valid.jsx"
    source.write_text("export default function Widget(){return <div/>}")
    assert check_syntax(source).ok


def test_invalid_style_value_has_localized_feedback(tmp_path: Path):
    source = tmp_path / "invalid.jsx"
    source.write_text(
        "export default function Widget(){\n"
        "  const style = { margin: 0 0 28 0 };\n"
        "  return <div style={style}/>;\n"
        "}\n"
    )
    result = check_syntax(source)
    message = format_syntax_error(result)
    assert not result.ok
    assert "SyntaxError at line 2" in message
    assert "margin: 0 0 28 0" in message
    assert "^" in message
