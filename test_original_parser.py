import io
import unittest
import zipfile

from original_parser import (
    UnsupportedFormat,
    detect_format,
    extract_archive_text,
    parse_docx,
    parse_xlsx,
)


def build_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


DOCX_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>关于开展档案安全检查的通知</w:t></w:r></w:p>
    <w:p><w:r><w:t>请各单位于本月内完成自查。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>部门</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>负责人</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>档案部</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>张三</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""

XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="值班表" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""

XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
</Relationships>
"""

XLSX_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>120</v></c></row>
  </sheetData>
</worksheet>
"""

XLSX_SHARED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
  <si><t>分支</t></si><si><t>值班人</t></si><si><t>南京运营中心</t></si>
</sst>
"""


def docx_bytes():
    return build_zip({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": DOCX_XML,
    })


def xlsx_bytes():
    return build_zip({
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": XLSX_WORKBOOK,
        "xl/_rels/workbook.xml.rels": XLSX_RELS,
        "xl/sharedStrings.xml": XLSX_SHARED,
        "xl/worksheets/sheet1.xml": XLSX_SHEET,
    })


class FormatDetectionTests(unittest.TestCase):
    def test_detects_office_pdf_image_and_text(self):
        self.assertEqual(detect_format(docx_bytes()), "docx")
        self.assertEqual(detect_format(xlsx_bytes()), "xlsx")
        self.assertEqual(detect_format(b"%PDF-1.7 broken"), "pdf")
        self.assertEqual(detect_format(b"\x89PNG\r\n\x1a\nrest"), "png")
        self.assertEqual(detect_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"), "ole")
        self.assertEqual(detect_format("纯文本内容".encode("utf-8"), "a.txt"), "text")


class DocxParserTests(unittest.TestCase):
    def test_paragraphs_and_table_rows_keep_sources(self):
        fragments = parse_docx(docx_bytes())
        texts = [f["text"] for f in fragments]
        self.assertIn("关于开展档案安全检查的通知", texts)
        self.assertIn("部门 | 负责人", texts)
        self.assertIn("档案部 | 张三", texts)
        paragraph = next(f for f in fragments if f["text"].endswith("通知"))
        self.assertEqual(paragraph["source"]["paragraph"], 1)
        table_row = next(f for f in fragments if f["text"] == "档案部 | 张三")
        self.assertEqual(table_row["source"]["table"], 1)
        self.assertEqual(table_row["source"]["row"], 2)


class XlsxParserTests(unittest.TestCase):
    def test_rows_keep_sheet_row_and_cell_range(self):
        fragments = parse_xlsx(xlsx_bytes())
        self.assertEqual(len(fragments), 2)
        self.assertEqual(fragments[0]["source"]["sheet"], "值班表")
        self.assertEqual(fragments[0]["source"]["row"], 1)
        self.assertEqual(fragments[0]["source"]["cellRange"], "A1:B1")
        self.assertIn("A1: 分支", fragments[0]["text"])
        self.assertIn("B1: 值班人", fragments[0]["text"])
        self.assertIn("南京运营中心", fragments[1]["text"])


class UnsupportedFormatTests(unittest.TestCase):
    def test_pdf_without_usable_component_is_explicit(self):
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(b"%PDF-1.7 not-a-real-pdf", "a.pdf")

    def test_image_without_ocr_is_explicit(self):
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(b"\x89PNG\r\n\x1a\nfake", "a.png")

    def test_legacy_office_is_explicit(self):
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake", "a.doc")

    def test_unknown_binary_is_explicit(self):
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(b"\x00\x01\x02\x03binary", "a.bin")


class ContainerParserTests(unittest.TestCase):
    def test_zip_container_extracts_text_and_nested_office(self):
        payload = build_zip({
            "readme.txt": "第一行\n第二行",
            "form.docx": docx_bytes(),
        })
        result = extract_archive_text(payload, "bundle.zip")
        self.assertEqual(result["format"], "zip")
        texts = [f["text"] for f in result["fragments"]]
        self.assertIn("第一行\n第二行", texts)
        self.assertIn("关于开展档案安全检查的通知", texts)
        nested = next(f for f in result["fragments"] if "通知" in f["text"])
        self.assertEqual(nested["source"]["path"], "form.docx")

    def test_zip_unsupported_member_is_reported_not_faked(self):
        payload = build_zip({"photo.png": b"\x89PNG\r\n\x1a\nfake", "a.txt": "文本"})
        result = extract_archive_text(payload, "bundle.zip")
        self.assertTrue(any("photo.png" in warning for warning in result["warnings"]))
        self.assertEqual([f["text"] for f in result["fragments"]], ["文本"])

    def test_character_limit_truncates_and_warns(self):
        payload = build_zip({"a.txt": "x" * 500})
        result = extract_archive_text(payload, "bundle.zip", max_chars=100)
        self.assertEqual(sum(len(f["text"]) for f in result["fragments"]), 100)
        self.assertTrue(any("截断" in warning for warning in result["warnings"]))

    def test_zip_bomb_ratio_is_rejected(self):
        payload = build_zip({"big.bin": b"0" * (2 * 1024 * 1024)})
        with self.assertRaises(UnsupportedFormat):
            extract_archive_text(payload, "bundle.zip")


if __name__ == "__main__":
    unittest.main()
