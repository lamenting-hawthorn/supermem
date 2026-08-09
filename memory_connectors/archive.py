"""Bounded extraction for hostile connector ZIP exports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat
import struct
from typing import BinaryIO
import zipfile

from supermem.config import SUPERMEM_FILE_SIZE_LIMIT, SUPERMEM_MEMORY_SIZE_LIMIT


@dataclass(frozen=True)
class ArchiveLimits:
    """Resource ceilings applied before and during ZIP extraction."""

    max_members: int = 10_000
    max_member_bytes: int = SUPERMEM_MEMORY_SIZE_LIMIT
    max_parsed_text_bytes: int = SUPERMEM_FILE_SIZE_LIMIT
    max_total_bytes: int = SUPERMEM_MEMORY_SIZE_LIMIT
    max_central_directory_bytes: int = 8 * 1024 * 1024
    max_compression_ratio: float = 200.0


DEFAULT_ARCHIVE_LIMITS = ArchiveLimits()
_COPY_CHUNK_BYTES = 64 * 1024
_MAX_ZIP_COMMENT_BYTES = 0xFFFF
_EOCD_RECORD = struct.Struct("<4s4H2LH")
_CENTRAL_DIRECTORY_RECORD = struct.Struct("<4s6H3L5H2L")
_ZIP64_MEMBER_COUNT = 0xFFFF
_ZIP64_FIELD = 0xFFFFFFFF
_PARSED_TEXT_SUFFIXES = frozenset({".csv", ".markdown", ".md"})


def _safe_relative_path(filename: str) -> Path:
    normalized = filename.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not normalized
        or member.is_absolute()
        or ".." in member.parts
        or (member.parts and member.parts[0].endswith(":"))
    ):
        raise ValueError(f"archive member has unsafe path: {filename!r}")
    return Path(*member.parts)


def _member_byte_limit(filename: str, limits: ArchiveLimits) -> tuple[int, str]:
    """Keep parsed text small while allowing bounded connector attachments."""
    if (
        PurePosixPath(filename.replace("\\", "/")).suffix.lower()
        in _PARSED_TEXT_SUFFIXES
    ):
        return min(limits.max_member_bytes, limits.max_parsed_text_bytes), "parsed text"
    return limits.max_member_bytes, "archive"


def _find_eocd(tail: bytes, archive_name: str) -> tuple[int, tuple[int, ...]]:
    """Find an EOCD whose comment reaches the actual end of the archive."""
    search_end = len(tail)
    saw_eocd_signature = False
    while True:
        eocd_offset = tail.rfind(b"PK\x05\x06", 0, search_end)
        if eocd_offset < 0:
            break
        saw_eocd_signature = True
        if eocd_offset + _EOCD_RECORD.size <= len(tail):
            fields = _EOCD_RECORD.unpack_from(tail, eocd_offset)
            comment_bytes = fields[-1]
            if eocd_offset + _EOCD_RECORD.size + comment_bytes == len(tail):
                return eocd_offset, fields[1:]
        search_end = eocd_offset
    if saw_eocd_signature:
        raise ValueError(f"archive has malformed ZIP end metadata: {archive_name!r}")
    raise ValueError(f"archive has no valid ZIP end record: {archive_name!r}")


def _validate_central_directory(
    directory: bytes,
    *,
    archive_name: str,
    member_count: int,
    archive_offset: int,
    central_directory_start: int,
    limits: ArchiveLimits,
) -> None:
    """Validate bounded central-directory records before ``ZipFile`` sees them."""
    cursor = 0
    actual_member_count = 0
    while cursor < len(directory):
        if cursor + _CENTRAL_DIRECTORY_RECORD.size > len(directory):
            raise ValueError(
                f"archive has malformed central directory metadata: {archive_name!r}"
            )
        (
            signature,
            _created_version,
            _needed_version,
            _flags,
            _compression,
            _modified_time,
            _modified_date,
            _crc,
            compressed_bytes,
            uncompressed_bytes,
            filename_bytes,
            extra_bytes,
            comment_bytes,
            disk_number,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = _CENTRAL_DIRECTORY_RECORD.unpack_from(directory, cursor)
        if signature != b"PK\x01\x02":
            raise ValueError(
                f"archive has malformed central directory metadata: {archive_name!r}"
            )
        if (
            compressed_bytes == _ZIP64_FIELD
            or uncompressed_bytes == _ZIP64_FIELD
            or local_header_offset == _ZIP64_FIELD
            or disk_number == _ZIP64_MEMBER_COUNT
        ):
            raise ValueError(f"Zip64 archive metadata is unsupported: {archive_name!r}")
        record_bytes = (
            _CENTRAL_DIRECTORY_RECORD.size
            + filename_bytes
            + extra_bytes
            + comment_bytes
        )
        if cursor + record_bytes > len(directory):
            raise ValueError(
                f"archive has malformed central directory metadata: {archive_name!r}"
            )
        absolute_local_header = archive_offset + local_header_offset
        if not 0 <= absolute_local_header < central_directory_start:
            raise ValueError(
                f"archive local-header offset is outside archive bounds: {archive_name!r}"
            )
        actual_member_count += 1
        if actual_member_count > limits.max_members:
            raise ValueError(
                f"archive has {actual_member_count} members; limit is {limits.max_members}"
            )
        cursor += record_bytes
    if actual_member_count != member_count:
        raise ValueError(
            "archive central directory member count does not match "
            f"ZIP end metadata: {archive_name!r}"
        )


def _validate_zip_before_opening(
    source: BinaryIO,
    *,
    archive_name: str,
    limits: ArchiveLimits,
) -> None:
    """Bound central-directory work before ``zipfile.ZipFile`` parses it."""
    source.seek(0, 2)
    archive_bytes = source.tell()
    if archive_bytes > limits.max_total_bytes:
        raise ValueError(
            f"archive file exceeds {limits.max_total_bytes} bytes: {archive_name!r}"
        )
    if archive_bytes < _EOCD_RECORD.size:
        raise ValueError(
            f"archive is too small to contain ZIP metadata: {archive_name!r}"
        )

    tail_bytes = min(archive_bytes, _EOCD_RECORD.size + _MAX_ZIP_COMMENT_BYTES)
    source.seek(archive_bytes - tail_bytes)
    tail = source.read(tail_bytes)
    eocd_offset, fields = _find_eocd(tail, archive_name)
    (
        disk_number,
        central_directory_disk,
        member_count_on_disk,
        member_count,
        central_directory_bytes,
        central_directory_offset,
        _comment_bytes,
    ) = fields
    if (
        member_count_on_disk == _ZIP64_MEMBER_COUNT
        or member_count == _ZIP64_MEMBER_COUNT
        or central_directory_bytes == _ZIP64_FIELD
        or central_directory_offset == _ZIP64_FIELD
    ):
        raise ValueError(f"Zip64 archive metadata is unsupported: {archive_name!r}")
    if member_count > limits.max_members:
        raise ValueError(
            f"archive has {member_count} members; limit is {limits.max_members}"
        )
    if (
        disk_number != 0
        or central_directory_disk != 0
        or member_count_on_disk != member_count
    ):
        raise ValueError(
            f"archive uses unsupported multi-disk metadata: {archive_name!r}"
        )
    # A bounded member count still permits a large, attacker-controlled central
    # directory. Keep its pre-open allocation within the import memory budget.
    if central_directory_bytes > limits.max_central_directory_bytes:
        raise ValueError(
            "archive central directory exceeds "
            f"{limits.max_central_directory_bytes} bytes"
        )
    eocd_absolute_offset = archive_bytes - tail_bytes + eocd_offset
    central_directory_start = eocd_absolute_offset - central_directory_bytes
    archive_offset = central_directory_start - central_directory_offset
    if central_directory_start < 0 or archive_offset < 0:
        raise ValueError(
            f"archive has invalid central directory bounds: {archive_name!r}"
        )
    source.seek(central_directory_start)
    directory = source.read(central_directory_bytes)
    if len(directory) != central_directory_bytes:
        raise ValueError(f"archive has truncated central directory: {archive_name!r}")
    _validate_central_directory(
        directory,
        archive_name=archive_name,
        member_count=member_count,
        archive_offset=archive_offset,
        central_directory_start=central_directory_start,
        limits=limits,
    )


class _BoundedZipFile(zipfile.ZipFile):
    """A ``ZipFile`` that owns the already-validated source descriptor."""

    def __init__(self, source: BinaryIO) -> None:
        self._bounded_source = source
        super().__init__(source, "r")

    def close(self) -> None:
        try:
            super().close()
        finally:
            if not self._bounded_source.closed:
                self._bounded_source.close()


def open_bounded_zip(
    archive_path: str | Path,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> zipfile.ZipFile:
    """Validate and parse a ZIP through one owned source descriptor."""
    path = Path(archive_path)
    source = path.open("rb")
    try:
        _validate_zip_before_opening(source, archive_name=path.name, limits=limits)
        return _BoundedZipFile(source)
    except Exception:
        source.close()
        raise


def safe_extract_zip(
    archive: zipfile.ZipFile,
    destination: str | Path,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> None:
    """Extract a ZIP with path, type, count, size, and ratio enforcement."""
    members = archive.infolist()
    if len(members) > limits.max_members:
        raise ValueError(
            f"archive has {len(members)} members; limit is {limits.max_members}"
        )

    total_declared = 0
    for info in members:
        _safe_relative_path(info.filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        entry_type = stat.S_IFMT(mode)
        if info.is_dir():
            if entry_type and entry_type != stat.S_IFDIR:
                raise ValueError(
                    f"archive directory has unsafe type metadata: {info.filename!r}"
                )
        elif entry_type and entry_type != stat.S_IFREG:
            raise ValueError(f"archive member is not a regular file: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise ValueError(f"archive member is encrypted: {info.filename!r}")
        member_limit, member_kind = _member_byte_limit(info.filename, limits)
        if info.file_size > member_limit:
            raise ValueError(
                f"{member_kind} member exceeds {member_limit} bytes: "
                f"{info.filename!r}"
            )
        total_declared += info.file_size
        if total_declared > limits.max_total_bytes:
            raise ValueError(
                f"archive expands beyond {limits.max_total_bytes} total bytes"
            )
        if info.file_size and (
            info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio
        ):
            raise ValueError(
                f"archive member exceeds compression ratio limit: {info.filename!r}"
            )

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    total_written = 0
    for info in members:
        relative = _safe_relative_path(info.filename)
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"archive member escapes destination: {info.filename!r}"
            ) from exc

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        member_written = 0
        member_limit, member_kind = _member_byte_limit(info.filename, limits)
        try:
            with archive.open(info, "r") as source, target.open("xb") as output:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    member_written += len(chunk)
                    total_written += len(chunk)
                    if member_written > member_limit:
                        raise ValueError(
                            f"{member_kind} member exceeded extraction limit: "
                            f"{info.filename!r}"
                        )
                    if total_written > limits.max_total_bytes:
                        raise ValueError("archive exceeded total extraction limit")
                    output.write(chunk)
        except FileExistsError as exc:
            raise ValueError(
                f"archive contains a duplicate or conflicting path: {info.filename!r}"
            ) from exc
