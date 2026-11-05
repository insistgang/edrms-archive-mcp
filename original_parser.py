"""电子原文解析：按真实格式提取文本，保留页码、段落、表格等来源位置。

设计原则：
- 只用标准库能解析的格式给出正文；没有组件时明确报“不支持”，不返回伪造内容。
- 解析结果是不可信业务数据，只读取不执行；不处理宏、脚本和外部引用。
- 所有输出都带来源位置，便于大模型引用和人工复核。
"""

import os
import posixpath
import shlex
import stat
import subprocess
import tempfile
from contextlib import contextmanager
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List, Optional, Tuple

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 500
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_TEXT_CHARACTERS = 200000

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".xml", ".html", ".js", ".css"})
OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class UnsupportedFormat(Exception):
    """没有可用解析组件时抛出，调用方应把它转成明确的可读错误，而不是空正文。"""

    def __init__(self, message: str, detected_format: str = "unknown"):
        super().__init__(message)
        self.detected_format = detected_format


def detect_format(payload: bytes, file_name: str = "") -> str:
    if not isinstance(payload, bytes) or not payload:
        return "empty"
    if payload.startswith(b"PK\x03\x04") or payload.startswith(b"PK\x05\x06"):
        return _detect_zip_member_format(payload)
    if payload.startswith(b"%PDF-"):
        return "pdf"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return "gif"
    if payload.startswith(b"II*\x00") or payload.startswith(b"MM\x00*"):
        return "tiff"
    if payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    extension = os.path.splitext(file_name or "")[1].lower()
    if extension in (".txt", ".md", ".csv", ".json", ".xml", ".html"):
        return "text"
    if extension in (".doc", ".xls", ".ppt"):
        return "ole"
    return "unknown"


def _detect_zip_member_format(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(_as_stream(payload)) as archive:
            names = set(archive.namelist())
    except Exception:
        return "zip"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return "zip"


def _as_stream(payload: bytes):
    import io
    return io.BytesIO(payload)


def _safe_member_path(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    if not name or "\x00" in name or name != info.filename or "\\" in name:
        raise UnsupportedFormat("压缩包成员路径不安全", "zip")
    path = name[:-1] if name.endswith("/") else name
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        raise UnsupportedFormat("压缩包成员路径不安全", "zip")
    return path


def read_zip_members(
    payload: bytes,
    max_bytes: int = MAX_ARCHIVE_BYTES,
    max_members: int = MAX_MEMBERS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    extensions: Optional[frozenset] = None
) -> List[Tuple[str, bytes]]:
    """按安全规则读取压缩包成员，返回 [(路径, 字节)]。不执行任何成员内容。"""
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise UnsupportedFormat("原文超过允许的大小上限", "zip")
    try:
        with zipfile.ZipFile(_as_stream(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > max_members:
                raise UnsupportedFormat("压缩包成员数量超过上限", "zip")
            if sum(info.file_size for info in infos) > max_total_bytes:
                raise UnsupportedFormat("压缩包展开后的总大小超过上限", "zip")
            seen = set()
            members: List[Tuple[str, bytes]] = []
            for info in infos:
                path = _safe_member_path(info)
                if path in seen:
                    raise UnsupportedFormat("压缩包存在重复成员路径", "zip")
                seen.add(path)
                if info.flag_bits & 1:
                    raise UnsupportedFormat("不支持加密压缩包", "zip")
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind == stat.S_IFLNK:
                    raise UnsupportedFormat("不支持符号链接成员", "zip")
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise UnsupportedFormat("压缩包含特殊成员", "zip")
                if info.file_size < 0 or info.file_size > max_member_bytes:
                    raise UnsupportedFormat("压缩包成员超过大小上限", "zip")
                if info.compress_size < 0 or info.file_size > MAX_COMPRESSION_RATIO * max(1, info.compress_size):
                    raise UnsupportedFormat("压缩包成员压缩比异常", "zip")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise UnsupportedFormat("压缩包使用了不支持的压缩算法", "zip")
                if info.is_dir():
                    continue
                if extensions is not None and os.path.splitext(path)[1].lower() not in extensions:
                    continue
                with archive.open(info) as member:
                    data = member.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise UnsupportedFormat("压缩包成员实际大小与声明不一致", "zip")
                members.append((path, data))
            return members
    except UnsupportedFormat:
        raise
    except Exception as exc:
        raise UnsupportedFormat("压缩包损坏或无法安全读取：%s" % type(exc).__name__, "zip")


@contextmanager
def _validated_office_archive(payload: bytes):
    members = dict(read_zip_members(payload))

    class Archive:
        def namelist(self):
            return list(members)

        def read(self, path):
            return members[path]

    try:
        yield Archive()
    except UnsupportedFormat:
        raise
    except (ET.ParseError, KeyError, ValueError, TypeError) as exc:
        raise UnsupportedFormat("Office 文档结构无效：%s" % type(exc).__name__, "office") from exc


def _parse_xml(data: bytes) -> ET.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise UnsupportedFormat("不支持带 DTD 或实体定义的 XML", "xml")
    return ET.fromstring(data)


def _paragraph_text(node: ET.Element) -> str:
    parts: List[str] = []
    for element in node.iter():
        if element.tag == W + "t" and element.text:
            parts.append(element.text)
        elif element.tag == W + "tab":
            parts.append(" ")
        elif element.tag == W + "br":
            parts.append("\n")
    return "".join(parts).strip()


def parse_docx(payload: bytes) -> List[Dict[str, Any]]:
    fragments: List[Dict[str, Any]] = []
    with _validated_office_archive(payload) as archive:
        if "word/document.xml" not in archive.namelist():
            raise UnsupportedFormat("docx 缺少 word/document.xml", "docx")
        root = _parse_xml(archive.read("word/document.xml"))
    body = root.find(W + "body")
    if body is None:
        raise UnsupportedFormat("docx 结构无法识别", "docx")
    paragraph_index = 0
    table_index = 0
    for child in body:
        if child.tag == W + "p":
            text = _paragraph_text(child)
            if text:
                paragraph_index += 1
                fragments.append({"text": text, "source": {"paragraph": paragraph_index, "table": None}})
        elif child.tag == W + "tbl":
            table_index += 1
            for row_index, row in enumerate(child.findall(W + "tr"), start=1):
                cells = []
                for cell in row.findall(W + "tc"):
                    cells.append(" ".join(_paragraph_text(p) for p in cell.findall(W + "p")).strip())
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    fragments.append({
                        "text": line,
                        "source": {"paragraph": None, "table": table_index, "row": row_index, "cells": len(cells)}
                    })
    return fragments


def _shared_strings(archive) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _parse_xml(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(S + "t")) for item in root.findall(S + "si")]


def _sheet_targets(archive) -> List[Tuple[str, str]]:
    workbook = _parse_xml(archive.read("xl/workbook.xml"))
    rels: Dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        rel_root = _parse_xml(archive.read("xl/_rels/workbook.xml.rels"))
        for relationship in rel_root:
            target = relationship.get("Target") or ""
            if relationship.get("TargetMode", "").lower() == "external" or ":" in target or "\\" in target:
                raise UnsupportedFormat("工作簿包含外部或不安全的引用", "xlsx")
            rels[relationship.get("Id")] = target
    targets: List[Tuple[str, str]] = []
    for sheet in workbook.iter(S + "sheet"):
        name = sheet.get("name") or "Sheet"
        target = rels.get(sheet.get(R + "id") or "")
        if not target:
            raise UnsupportedFormat("工作表缺少内部关系映射", "xlsx")
        if target.startswith("/"):
            path = posixpath.normpath(target.lstrip("/"))
        else:
            path = posixpath.normpath("xl/" + target)
        if not path.startswith("xl/") or path not in archive.namelist():
            raise UnsupportedFormat("工作表路径无效或目标不存在", "xlsx")
        targets.append((name, path))
    return targets


def parse_xlsx(payload: bytes) -> List[Dict[str, Any]]:
    fragments: List[Dict[str, Any]] = []
    with _validated_office_archive(payload) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            raise UnsupportedFormat("xlsx 缺少 xl/workbook.xml", "xlsx")
        shared = _shared_strings(archive)
        for sheet_name, sheet_path in _sheet_targets(archive):
            root = _parse_xml(archive.read(sheet_path))
            for row in root.iter(S + "row"):
                cells: List[str] = []
                references: List[str] = []
                for cell in row.findall(S + "c"):
                    reference = cell.get("r") or ""
                    cell_type = cell.get("t")
                    value_node = cell.find(S + "v")
                    inline = cell.find(S + "is")
                    if cell_type == "s" and value_node is not None and value_node.text is not None:
                        try:
                            index = int(value_node.text)
                            if index < 0:
                                raise ValueError("共享字符串索引不能为负")
                            value = shared[index]
                        except (ValueError, IndexError) as exc:
                            raise UnsupportedFormat("工作表共享字符串索引无效", "xlsx") from exc
                    elif cell_type == "inlineStr" and inline is not None:
                        value = "".join(node.text or "" for node in inline.iter(S + "t"))
                    elif value_node is not None:
                        value = value_node.text or ""
                    else:
                        value = ""
                    formula = cell.find(S + "f")
                    if formula is not None and formula.text:
                        value = "%s [公式: %s]" % (value, formula.text)
                    if value != "":
                        cells.append("%s: %s" % (reference, value))
                        references.append(reference)
                if cells:
                    fragments.append({
                        "text": " | ".join(cells),
                        "source": {
                            "sheet": sheet_name,
                            "row": int(row.get("r") or 0),
                            "cellRange": references[0] + ":" + references[-1] if references else None
                        }
                    })
    return fragments


def parse_pdf(payload: bytes) -> List[Dict[str, Any]]:
    reader_class = None
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name)
            reader_class = getattr(module, "PdfReader")
            break
        except Exception:
            continue
    if reader_class is None:
        raise UnsupportedFormat(
            "PDF 文本提取需要安装 pypdf，或由客户提供可调用的解析组件；当前未安装，不返回推测正文。",
            "pdf"
        )
    try:
        reader = reader_class(_as_stream(payload))
        fragments = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                fragments.append({"text": text, "source": {"page": number, "paragraph": None}})
        return fragments
    except UnsupportedFormat:
        raise
    except Exception as exc:
        raise UnsupportedFormat("PDF 解析失败：%s" % type(exc).__name__, "pdf")


def parse_image(payload: bytes, file_name: str = "") -> List[Dict[str, Any]]:
    command = os.getenv("MCP_OCR_COMMAND", "").strip()
    if not command:
        raise UnsupportedFormat(
            "图片或扫描件需要 OCR 组件；请提供 MCP_OCR_COMMAND 或由客户提供 OCR 接口，当前不返回推测文字。",
            "image"
        )
    handle, temp_path = tempfile.mkstemp(prefix="edrms-ocr-", suffix=os.path.splitext(file_name or ".bin")[1] or ".bin")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        arguments = shlex.split(command)
        has_placeholder = any("{path}" in argument for argument in arguments)
        arguments = [argument.replace("{path}", temp_path) for argument in arguments]
        if not has_placeholder:
            arguments.append(temp_path)
        completed = subprocess.run(
            arguments,
            capture_output=True,
            timeout=float(os.getenv("MCP_OCR_TIMEOUT_SECONDS", "60")),
            check=False
        )
        if completed.returncode != 0:
            raise UnsupportedFormat("OCR 组件执行失败，退出码 %d" % completed.returncode, "image")
        text = completed.stdout.decode("utf-8", "replace").strip()
        if not text:
            raise UnsupportedFormat("OCR 组件未返回文字", "image")
        return [{"text": text, "source": {"page": 1, "paragraph": None}}]
    except UnsupportedFormat:
        raise
    except Exception as exc:
        raise UnsupportedFormat("OCR 调用失败：%s" % type(exc).__name__, "image")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def parse_text(payload: bytes) -> List[Dict[str, Any]]:
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsupportedFormat("文本原文不是 UTF-8 编码，无法安全解码", "text")
    fragments = []
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped:
            fragments.append({"text": stripped, "source": {"line": number}})
    return fragments


def extract_archive_text(
    payload: bytes,
    file_name: str = "",
    max_chars: int = MAX_TEXT_CHARACTERS
) -> Dict[str, Any]:
    """按真实格式提取原文，返回 fragments、format、parser 和 warnings。"""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ARCHIVE_BYTES:
        raise UnsupportedFormat("原文为空或超过允许大小", "unknown")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_TEXT_CHARACTERS:
        raise UnsupportedFormat("原文字符上限无效", "unknown")
    detected = detect_format(payload, file_name)
    warnings: List[str] = []
    fragments: List[Dict[str, Any]] = []
    parser = "unknown"

    if detected in ("docx", "xlsx", "pptx", "pdf", "png", "jpeg", "gif", "tiff", "ole", "text"):
        if detected == "docx":
            fragments, parser = parse_docx(payload), "docx_ooxml"
        elif detected == "xlsx":
            fragments, parser = parse_xlsx(payload), "xlsx_ooxml"
        elif detected == "pptx":
            raise UnsupportedFormat("PPTX 解析尚未实现，需要客户提供组件", "pptx")
        elif detected == "pdf":
            fragments, parser = parse_pdf(payload), "pypdf"
        elif detected in ("png", "jpeg", "gif", "tiff"):
            fragments, parser = parse_image(payload, file_name), "ocr_external"
            warnings.append("图片文字来自外部 OCR 组件，识别结果需人工复核。")
        elif detected == "ole":
            raise UnsupportedFormat("旧版 doc/xls 格式需要转换组件，当前不支持", "ole")
        else:
            fragments, parser = parse_text(payload), "utf8_text"
    elif detected == "zip":
        members = read_zip_members(payload)
        for path, data in members:
            member_format = detect_format(data, path)
            if member_format in ("docx", "xlsx"):
                try:
                    nested = parse_docx(data) if member_format == "docx" else parse_xlsx(data)
                except UnsupportedFormat as exc:
                    warnings.append("成员 %s 解析失败：%s" % (path, exc))
                    continue
                for fragment in nested:
                    fragment["source"]["path"] = path
                    fragments.append(fragment)
            elif member_format == "text" or os.path.splitext(path)[1].lower() in TEXT_EXTENSIONS:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    warnings.append("成员 %s 不是 UTF-8 文本，已跳过。" % path)
                    continue
                if "\x00" in content:
                    warnings.append("成员 %s 含二进制 NUL，已跳过。" % path)
                    continue
                if content.strip():
                    fragments.append({
                        "text": content,
                        "source": {"path": path, "line": 1, "lineCount": len(content.splitlines())}
                    })
            else:
                warnings.append("成员 %s 为 %s，未支持的格式已跳过。" % (path, member_format))
        parser = "zip_container"
    else:
        raise UnsupportedFormat("无法识别原文格式（%s），未返回推测正文。" % detected, detected)

    if not fragments:
        raise UnsupportedFormat("原文中没有可读取文本；扫描件需要 OCR，或所含格式尚不支持", detected)
    total = sum(len(fragment["text"]) for fragment in fragments)
    truncated = total > max_chars
    if truncated:
        kept: List[Dict[str, Any]] = []
        remaining = max_chars
        for fragment in fragments:
            if remaining <= 0:
                break
            text = fragment["text"]
            if len(text) > remaining:
                fragment = {**fragment, "text": text[:remaining]}
            kept.append(fragment)
            remaining -= len(fragment["text"])
        warnings.append("原文超过 %d 字符上限，已截断。" % max_chars)
        fragments = kept

    return {"format": detected, "parser": parser, "fragments": fragments,
            "truncated": truncated, "warnings": warnings}
