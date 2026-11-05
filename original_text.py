"""Read bounded ZIP members as untrusted text, without writing or executing them."""

import io
from pathlib import PurePosixPath
import stat
import zlib
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_MEMBERS = 100
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 100000
MAX_COMPRESSION_RATIO = 200
TEXT_EXTENSIONS = frozenset({".json", ".js", ".css", ".txt", ".md", ".csv", ".xml", ".html"})


def _member_path(info) -> str:
    name = info.orig_filename
    # ZipInfo truncates NUL-containing names; reject the original representation.
    if not name or "\x00" in name or name != info.filename or "\\" in name:
        raise ValueError("ZIP contains an unsafe member path")
    path = name[:-1] if name.endswith("/") else name
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        raise ValueError("ZIP contains an absolute or non-canonical member path")
    return path


def extract_zip_text(payload: bytes) -> list[dict[str, str]]:
    """Return UTF-8 text members in archive order, or raise ValueError.

    Only stored/deflated ZIPs are supported. Directories and extensions outside
    TEXT_EXTENSIONS (including nested ZIP, PDF, Office and image files) are
    deliberately omitted; they are still size/path/CRC checked. JavaScript,
    CSS and HTML are returned literally and never evaluated. No output is
    silently truncated. All validation completes before a result is returned.
    """
    if not isinstance(payload, bytes):
        raise ValueError("ZIP input must be bytes")
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("ZIP input exceeds the 2 MiB limit")
    if not payload.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise ValueError("Input is not a supported ZIP archive")

    try:
        with ZipFile(io.BytesIO(payload)) as zipped:
            members = zipped.infolist()
            if len(members) > MAX_MEMBERS:
                raise ValueError("ZIP contains more than 100 members")
            if sum(info.file_size for info in members) > MAX_TOTAL_BYTES:
                raise ValueError("ZIP declared uncompressed size exceeds 4 MiB")

            paths = set()
            validated = []
            for info in members:
                path = _member_path(info)
                if path in paths:
                    raise ValueError("ZIP contains duplicate member paths")
                paths.add(path)
                if info.flag_bits & 1:
                    raise ValueError("Encrypted ZIP members are not supported")
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind == stat.S_IFLNK:
                    raise ValueError("ZIP symbolic links are not supported")
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("ZIP contains an unsupported special member")
                if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("ZIP member exceeds the 1 MiB limit")
                if info.compress_size < 0 or info.file_size > MAX_COMPRESSION_RATIO * max(1, info.compress_size):
                    raise ValueError("ZIP member exceeds the 200:1 compression ratio limit")
                if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    raise ValueError("ZIP compression method is not supported")
                if info.is_dir() and info.file_size:
                    raise ValueError("ZIP directory contains unexpected data")
                validated.append((info, path))

            result = []
            characters = 0
            for info, path in validated:
                if info.is_dir():
                    continue
                # The declared size was bounded above. Reading one extra byte
                # also avoids treating a mismatched size as a complete result.
                with zipped.open(info) as member:
                    data = member.read(info.file_size + 1)
                if len(data) != info.file_size:
                    raise ValueError("ZIP member size does not match its declaration")
                if PurePosixPath(path).suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"ZIP text member is not UTF-8: {path}") from exc
                if "\x00" in content:
                    raise ValueError(f"ZIP text member contains binary NUL data: {path}")
                characters += len(content)
                if characters > MAX_TEXT_CHARACTERS:
                    raise ValueError("ZIP decoded text exceeds the 100000 character limit")
                result.append({"path": path, "content": content})
            return result
    except (BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        raise ValueError("ZIP is malformed or cannot be read safely") from exc
