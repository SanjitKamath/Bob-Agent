from __future__ import annotations

import json

import pytest
from bob_documents import extract
from bob_sdk.runtime import ToolError, check_declared


@pytest.fixture
def form_pdf(tmp_path):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "Given Name:")
    page.insert_text((72, 140), "Residential Status")

    def widget(name, kind, rect, **extra):
        item = pymupdf.Widget()
        item.field_name, item.field_type, item.rect = name, kind, pymupdf.Rect(*rect)
        for key, value in extra.items():
            setattr(item, key, value)
        page.add_widget(item)

    widget("first_name", pymupdf.PDF_WIDGET_TYPE_TEXT, (160, 88, 360, 104), text_maxlen=40)
    widget(
        "residency", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, (200, 128, 360, 144),
        choice_values=["Resident", "NRI"], field_value="Resident",
    )  # fmt: skip
    widget("agree", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (72, 170, 84, 182), field_label="I agree")
    path = tmp_path / "form.pdf"
    document.save(path)
    document.close()
    return path


def test_pdf_widgets_become_fields_with_labels(form_pdf):
    result = extract.pdf_fields(form_pdf)
    assert result["source"] == "widgets" and result["pages"] == 1 and result["count"] == 3
    by_name = {field["name"]: field for field in result["fields"]}
    assert by_name["first_name"] == {
        "name": "first_name", "label": "Given Name", "type": "text", "page": 1, "max_length": 40,
    }  # fmt: skip
    assert by_name["residency"]["type"] == "dropdown"
    assert by_name["residency"]["options"] == ["Resident", "NRI"]
    assert by_name["residency"]["label"] == "Residential Status"
    assert by_name["agree"]["label"] == "I agree" and by_name["agree"]["type"] == "checkbox"


def test_flat_pdf_falls_back_to_blanks_in_the_text(tmp_path):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    for offset, line in enumerate(
        [
            "Application form for a new account",
            "Full name: ______________",
            "PAN ..........",
            "Plain sentence.",
        ]
    ):
        page.insert_text((72, 100 + 24 * offset), line)
    path = tmp_path / "flat.pdf"
    document.save(path)
    document.close()
    result = extract.pdf_fields(path)
    assert result["source"] == "text"
    assert [(f["name"], f["label"]) for f in result["fields"]] == [
        ("full_name", "Full name"),
        ("pan", "PAN"),
    ]


def test_fields_from_text_patterns():
    text = "Name: ________\n[ ] I accept the terms\nDate of birth ........\nJust words here\n: ____"
    assert [(f["label"], f["type"]) for f in extract.fields_from_text(text, 2)] == [
        ("Name", "text"),
        ("I accept the terms", "checkbox"),
        ("Date of birth", "text"),
    ]
    assert extract.slug("Date of Birth (DD/MM)") == "date_of_birth_dd_mm"


def test_docx_parse_sections_tables_placeholders(tmp_path):
    import docx

    document = docx.Document()
    document.add_heading("Account Opening", level=1)
    document.add_paragraph("Dear {{ customer_name }}, your branch is ${branch}.")
    document.add_heading("Holder", level=2)
    document.add_paragraph("PAN: __________")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Field", "Value"
    table.cell(1, 0).text, table.cell(1, 1).text = "Nominee", "<<nominee>>"
    path = tmp_path / "letter.docx"
    document.save(str(path))
    result = extract.docx_parse(path)
    assert [(s["heading"], s["level"]) for s in result["sections"]] == [
        ("Account Opening", 1),
        ("Holder", 2),
    ]
    assert result["placeholders"] == ["customer_name", "branch", "nominee"]
    assert result["tables"] == [
        {"under": "Holder", "header": ["Field", "Value"], "rows": [["Nominee", "<<nominee>>"]]}
    ]
    assert result["fields"] == [{"name": "pan", "label": "PAN", "type": "text", "page": 1}]


def test_bad_files_are_reported(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_text("not a pdf")
    with pytest.raises(ToolError, match="cannot open PDF"):
        extract.pdf_fields(broken)
    with pytest.raises(ToolError, match="cannot open DOCX"):
        extract.docx_parse(broken)


def test_server_guards_paths_and_types(tmp_path, monkeypatch, form_pdf):
    from bob_documents import server

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"data_dir": str(tmp_path / "data"), "settings": {"read_roots": str(tmp_path)}})
    )
    monkeypatch.setenv("BOB_PLUGIN_CONFIG", str(config))
    monkeypatch.setattr(server, "_ctx", None)
    assert server.pdf_fields(str(form_pdf))["count"] == 3
    with pytest.raises(ToolError, match="expected one of"):
        server.docx_parse(str(form_pdf))
    with pytest.raises(ToolError, match="outside the folders"):
        server.pdf_fields("/etc/hosts.pdf")
    check_declared(server.server, server.MANIFEST)
