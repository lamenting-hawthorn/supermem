"""
Shared pytest fixtures for the Recall test suite.
"""
import os
import tempfile
import pytest


@pytest.fixture
def temp_memory_dir():
    """Create a temporary directory to use as a memory root during tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        yield tmpdir
        os.chdir(original_cwd)


@pytest.fixture
def memory_with_files(temp_memory_dir):
    """
    Provide a temp memory directory pre-populated with a user.md
    and one entity file, matching the Obsidian-style vault structure.
    """
    user_md = os.path.join(temp_memory_dir, "user.md")
    with open(user_md, "w") as f:
        f.write(
            "# User Information\n"
            "- user_name: Test User\n"
            "- location: Amsterdam\n\n"
            "## Relationships\n"
            "- colleague: [[entities/alice.md]]\n"
        )

    entities_dir = os.path.join(temp_memory_dir, "entities")
    os.makedirs(entities_dir, exist_ok=True)

    alice_md = os.path.join(entities_dir, "alice.md")
    with open(alice_md, "w") as f:
        f.write(
            "# Alice\n"
            "- relationship: Colleague\n"
            "- company: Acme Corp\n"
        )

    return temp_memory_dir
