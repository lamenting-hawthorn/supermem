import os
import uuid
from typing import Union

from agent.utils import check_size_limits
from supermem.privacy.filter import PrivacyFilter

_RAW_VAULT_ACCESS_DENIED = (
    "Error: Raw Agent vault inspection is unavailable pending a "
    "source-aware lifecycle broker."
)


def get_size(file_or_dir_path: str) -> int:
    """Deny raw file-size and directory-size inspection to Agent callers."""
    del file_or_dir_path
    raise PermissionError(_RAW_VAULT_ACCESS_DENIED)


def create_file(file_path: str, content: str = "") -> bool:
    """
    Create a new file in the memory with the given content (if any).
    First create a temporary file with the given content, check if
    the size limits are respected, if so, move the temporary file to
    the final destination.

    Args:
        file_path: The path to the file.
        content: The content of the file.

    Returns:
        True if the file was created successfully, False otherwise.
    """
    temp_file_path = None
    try:
        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(file_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        # Create a unique temporary file name in the same directory as the target file
        # This keeps the temp file within the restricted executor's allowed path.
        target_dir = os.path.dirname(os.path.abspath(file_path)) or "."
        temp_file_path = os.path.join(target_dir, f"temp_{uuid.uuid4().hex[:8]}.txt")

        with open(temp_file_path, "w") as f:
            f.write(content)

        if check_size_limits(temp_file_path):
            # Move the content to the final destination
            with open(file_path, "w") as f:
                f.write(content)
            os.remove(temp_file_path)
            return True
        else:
            os.remove(temp_file_path)
            raise Exception(f"File {file_path} is too large to create")
    except Exception as e:
        # Clean up temp file if it exists
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_exc:
                raise Exception(
                    f"Error removing temp file {temp_file_path}: {cleanup_exc}"
                )
        raise Exception(f"Error creating file {file_path}: {e}")


def create_dir(dir_path: str) -> bool:
    """
    Create a new directory in the memory.

    Args:
        dir_path: The path to the directory.

    Returns:
        True if the directory was created successfully, False otherwise.
    """
    try:
        os.makedirs(dir_path, exist_ok=True)
        return True
    except Exception:
        return False


def update_file(file_path: str, old_content: str, new_content: str) -> Union[bool, str]:
    """
    Simple find-and-replace update method for files.

    This is an easier alternative to write_to_file() that doesn't require
    creating git-style diffs. It performs a simple string replacement.

    Parameters
    ----------
    file_path : str
        Path to the file to update.
    old_content : str
        The exact text to find and replace in the file.
    new_content : str
        The text to replace old_content with.

    Returns
    -------
    Union[bool, str]
        True if successful, error message string if failed.

    Examples
    --------
    # Add a new row to a table
    old = "| TKT-1056  | 2024-09-25 | Late Delivery   | Resolved |"
    new = "| TKT-1056  | 2024-09-25 | Late Delivery   | Resolved |\\n| TKT-1057  | 2024-11-11 | Damaged Item    | Open     |"
    result = update_file("user.md", old, new)
    """
    try:
        # Read the current file content
        if not os.path.exists(file_path):
            return f"Error: File '{file_path}' does not exist"

        if not os.path.isfile(file_path):
            return f"Error: '{file_path}' is not a file"

        with open(file_path, "r") as f:
            current_content = f.read()
            private_was_redacted = bool(getattr(f, "_supermem_private_redacted", False))

        if private_was_redacted or PrivacyFilter.has_private(current_content):
            return (
                "Error: Files containing private blocks cannot be updated "
                "through the restricted agent tool."
            )

        # Check if old_content exists in the file
        if old_content not in current_content:
            # Provide helpful context about what wasn't found
            preview_length = 50
            preview = (
                old_content[:preview_length] + "..."
                if len(old_content) > preview_length
                else old_content
            )
            return f"Error: Could not find the specified content in the file. Looking for: '{preview}'"

        # Count occurrences to warn about multiple matches
        occurrences = current_content.count(old_content)
        if occurrences > 1:
            # Still proceed but warn the user
            print(
                f"Warning: Found {occurrences} occurrences of the content. Replacing only the first one."
            )

        # Perform the replacement (only first occurrence)
        updated_content = current_content.replace(old_content, new_content, 1)

        # Check if replacement actually changed anything
        if updated_content == current_content:
            return "Error: No changes were made to the file"

        # Write the updated content back
        with open(file_path, "w") as f:
            f.write(updated_content)

        return True

    except PermissionError as exc:
        return f"Error: {exc}"
    except Exception as e:
        return f"Error: Unexpected error - {str(e)}"


def read_file(file_path: str) -> str:
    """Deny raw vault-content inspection to Agent callers."""
    del file_path
    return _RAW_VAULT_ACCESS_DENIED


def list_files() -> str:
    """Deny raw vault-directory inspection to Agent callers."""
    return _RAW_VAULT_ACCESS_DENIED


def delete_file(file_path: str) -> bool:
    """
    Delete a file in the memory.

    Args:
        file_path: The path to the file.

    Returns:
        True if the file was deleted successfully, False otherwise.
    """
    try:
        os.remove(file_path)
        return True
    except Exception:
        return False


def go_to_link(link_string: str) -> str:
    """Deny linked vault-content inspection to Agent callers."""
    del link_string
    return _RAW_VAULT_ACCESS_DENIED


def check_if_file_exists(file_path: str) -> bool:
    """Deny Agent file-presence probes without revealing path metadata."""
    del file_path
    return False


def check_if_dir_exists(dir_path: str) -> bool:
    """Deny Agent directory-presence probes without revealing metadata."""
    del dir_path
    return False
