"""
Unit tests for agent/tools.py — the file operation API exposed to the agent.

These tests run directly against the tool functions (not through the sandbox),
exercising the actual file I/O logic with a temporary memory directory.
"""
import os
import pytest

# tools.py uses os.getcwd() as the memory root when called inside the sandbox.
# In tests we set cwd to a temp dir via the conftest fixtures.
from agent.tools import (
    create_file,
    create_dir,
    read_file,
    update_file,
    delete_file,
    list_files,
    go_to_link,
    check_if_file_exists,
    check_if_dir_exists,
    get_size,
)


# ---------------------------------------------------------------------------
# create_file
# ---------------------------------------------------------------------------

class TestCreateFile:
    def test_creates_file_with_content(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        result = create_file(path, "# Hello\nSome content.")
        assert result is True
        assert os.path.isfile(path)
        with open(path) as f:
            assert f.read() == "# Hello\nSome content."

    def test_creates_file_without_content(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "empty.md")
        result = create_file(path)
        assert result is True
        assert os.path.isfile(path)

    def test_creates_parent_directories(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "entities", "deep", "note.md")
        result = create_file(path, "nested")
        assert result is True
        assert os.path.isfile(path)

    def test_overwrites_existing_file(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "original")
        create_file(path, "updated")
        with open(path) as f:
            assert f.read() == "updated"


# ---------------------------------------------------------------------------
# create_dir
# ---------------------------------------------------------------------------

class TestCreateDir:
    def test_creates_directory(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "topics")
        result = create_dir(path)
        assert result is True
        assert os.path.isdir(path)

    def test_is_idempotent(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "topics")
        create_dir(path)
        result = create_dir(path)  # should not raise
        assert result is True

    def test_creates_nested_directories(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "a", "b", "c")
        result = create_dir(path)
        assert result is True
        assert os.path.isdir(path)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_existing_file(self, memory_with_files):
        path = os.path.join(memory_with_files, "user.md")
        content = read_file(path)
        assert "Test User" in content
        assert "Amsterdam" in content

    def test_returns_error_for_missing_file(self, temp_memory_dir):
        result = read_file(os.path.join(temp_memory_dir, "does_not_exist.md"))
        assert result.startswith("Error")

    def test_returns_error_for_directory(self, temp_memory_dir):
        result = read_file(temp_memory_dir)
        assert result.startswith("Error")


# ---------------------------------------------------------------------------
# update_file
# ---------------------------------------------------------------------------

class TestUpdateFile:
    def test_replaces_content(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "Hello World")
        result = update_file(path, "World", "Recall")
        assert result is True
        with open(path) as f:
            assert f.read() == "Hello Recall"

    def test_returns_error_when_old_content_not_found(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "Hello World")
        result = update_file(path, "Nonexistent text", "Replacement")
        assert isinstance(result, str)
        assert "Error" in result

    def test_returns_error_for_missing_file(self, temp_memory_dir):
        result = update_file(
            os.path.join(temp_memory_dir, "ghost.md"), "old", "new"
        )
        assert isinstance(result, str)
        assert "Error" in result

    def test_replaces_only_first_occurrence(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "a a a")
        update_file(path, "a", "b")
        with open(path) as f:
            assert f.read() == "b a a"

    def test_multiline_replacement(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "line1\nline2\nline3")
        result = update_file(path, "line2\nline3", "new_line2\nnew_line3")
        assert result is True
        with open(path) as f:
            assert f.read() == "line1\nnew_line2\nnew_line3"


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------

class TestDeleteFile:
    def test_deletes_existing_file(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        create_file(path, "bye")
        result = delete_file(path)
        assert result is True
        assert not os.path.exists(path)

    def test_returns_false_for_missing_file(self, temp_memory_dir):
        result = delete_file(os.path.join(temp_memory_dir, "ghost.md"))
        assert result is False


# ---------------------------------------------------------------------------
# check_if_file_exists / check_if_dir_exists
# ---------------------------------------------------------------------------

class TestExistenceChecks:
    def test_file_exists(self, memory_with_files):
        path = os.path.join(memory_with_files, "user.md")
        assert check_if_file_exists(path) is True

    def test_file_not_exists(self, temp_memory_dir):
        assert check_if_file_exists(os.path.join(temp_memory_dir, "nope.md")) is False

    def test_dir_not_treated_as_file(self, temp_memory_dir):
        assert check_if_file_exists(temp_memory_dir) is False

    def test_dir_exists(self, memory_with_files):
        entities = os.path.join(memory_with_files, "entities")
        assert check_if_dir_exists(entities) is True

    def test_dir_not_exists(self, temp_memory_dir):
        assert check_if_dir_exists(os.path.join(temp_memory_dir, "nope")) is False

    def test_file_not_treated_as_dir(self, memory_with_files):
        path = os.path.join(memory_with_files, "user.md")
        assert check_if_dir_exists(path) is False


# ---------------------------------------------------------------------------
# go_to_link
# ---------------------------------------------------------------------------

class TestGoToLink:
    def test_follows_obsidian_link(self, memory_with_files):
        # The cwd is set to temp_memory_dir by the fixture
        content = go_to_link("[[entities/alice]]")
        assert "Alice" in content
        assert "Acme Corp" in content

    def test_follows_obsidian_link_with_md_extension(self, memory_with_files):
        content = go_to_link("[[entities/alice.md]]")
        assert "Alice" in content

    def test_returns_error_for_missing_link(self, temp_memory_dir):
        result = go_to_link("[[entities/nobody]]")
        assert result.startswith("Error")

    def test_follows_plain_path(self, memory_with_files):
        path = os.path.join(memory_with_files, "user.md")
        content = go_to_link(path)
        assert "Test User" in content


# ---------------------------------------------------------------------------
# get_size
# ---------------------------------------------------------------------------

class TestGetSize:
    def test_file_size(self, temp_memory_dir):
        path = os.path.join(temp_memory_dir, "note.md")
        content = "Hello, world!"
        create_file(path, content)
        size = get_size(path)
        assert size == len(content.encode())

    def test_directory_size_is_sum_of_files(self, temp_memory_dir):
        create_file(os.path.join(temp_memory_dir, "a.md"), "aaa")
        create_file(os.path.join(temp_memory_dir, "b.md"), "bbbb")
        size = get_size(temp_memory_dir)
        assert size >= 7  # at least 3 + 4 bytes

    def test_raises_for_nonexistent_path(self, temp_memory_dir):
        with pytest.raises(FileNotFoundError):
            get_size(os.path.join(temp_memory_dir, "ghost.md"))


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_shows_files_in_tree(self, memory_with_files):
        # cwd is set to memory_with_files by conftest
        result = list_files()
        assert "user.md" in result
        assert "entities" in result
        assert "alice.md" in result

    def test_empty_directory(self, temp_memory_dir):
        result = list_files()
        assert isinstance(result, str)
        # Should not crash; may return just the root line
        assert "./" in result
