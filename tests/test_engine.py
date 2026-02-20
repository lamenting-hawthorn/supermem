"""
Unit tests for agent/engine.py — the sandboxed subprocess executor.

Tests verify that code executes correctly, that timeouts are enforced,
and that path restrictions prevent access outside the allowed directory.
"""
import os
import pytest

from agent.engine import execute_sandboxed_code


class TestBasicExecution:
    def test_simple_expression(self):
        locals_dict, error = execute_sandboxed_code("x = 1 + 1")
        assert error == "" or error is None
        assert locals_dict["x"] == 2

    def test_string_operations(self):
        locals_dict, error = execute_sandboxed_code("result = 'hello ' + 'world'")
        assert locals_dict["result"] == "hello world"

    def test_list_operations(self):
        code = "items = [1, 2, 3]\ntotal = sum(items)"
        locals_dict, error = execute_sandboxed_code(code)
        assert locals_dict["total"] == 6

    def test_multiple_variables(self):
        code = "a = 10\nb = 20\nc = a + b"
        locals_dict, error = execute_sandboxed_code(code)
        assert locals_dict["c"] == 30

    def test_returns_empty_dict_for_no_locals(self):
        locals_dict, error = execute_sandboxed_code("pass")
        assert isinstance(locals_dict, dict)


class TestErrorHandling:
    def test_captures_exception_in_error_message(self):
        locals_dict, error = execute_sandboxed_code("raise ValueError('test error')")
        assert error is not None
        assert "ValueError" in error or "test error" in error

    def test_zero_division_error(self):
        locals_dict, error = execute_sandboxed_code("x = 1 / 0")
        assert error is not None
        assert "ZeroDivision" in error or "division by zero" in error

    def test_name_error(self):
        locals_dict, error = execute_sandboxed_code("x = undefined_variable")
        assert error is not None

    def test_syntax_error(self):
        locals_dict, error = execute_sandboxed_code("def broken(")
        # Should return an error (returncode != 0 from subprocess)
        assert error is not None


class TestTimeout:
    def test_timeout_is_enforced(self):
        # Infinite loop should be killed by timeout
        locals_dict, error = execute_sandboxed_code(
            "while True: pass", timeout=2
        )
        assert error is not None
        assert "Timeout" in error or "timeout" in error or "TimeoutError" in error


class TestPathRestriction:
    def test_allowed_path_write_and_read(self, tmp_path):
        allowed = str(tmp_path)
        code = (
            f"with open('{allowed}/test.txt', 'w') as f:\n"
            f"    f.write('hello')\n"
            f"with open('{allowed}/test.txt', 'r') as f:\n"
            f"    content = f.read()\n"
        )
        locals_dict, error = execute_sandboxed_code(code, allowed_path=allowed)
        assert not error
        assert locals_dict.get("content") == "hello"

    def test_access_outside_allowed_path_is_denied(self, tmp_path):
        allowed = str(tmp_path / "memory")
        os.makedirs(allowed, exist_ok=True)

        # Try to write to the parent directory, which is outside allowed
        outside = str(tmp_path / "secret.txt")
        code = (
            f"try:\n"
            f"    with open('{outside}', 'w') as f:\n"
            f"        f.write('escaped')\n"
            f"    escaped = True\n"
            f"except PermissionError:\n"
            f"    escaped = False\n"
        )
        locals_dict, error = execute_sandboxed_code(code, allowed_path=allowed)
        # The sandbox should block the write
        assert locals_dict.get("escaped") is False


class TestAvailableFunctions:
    def test_custom_function_is_callable(self):
        def add(a, b):
            return a + b

        locals_dict, error = execute_sandboxed_code(
            "result = add(3, 4)",
            available_functions={"add": add},
        )
        assert not error
        assert locals_dict["result"] == 7

    def test_import_module_makes_functions_available(self, tmp_path):
        # agent.tools should be importable in the project context
        allowed = str(tmp_path)
        locals_dict, error = execute_sandboxed_code(
            "result = check_if_file_exists('nonexistent.md')",
            allowed_path=allowed,
            import_module="agent.tools",
        )
        assert not error
        assert locals_dict["result"] is False
