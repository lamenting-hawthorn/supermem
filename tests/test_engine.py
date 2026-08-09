"""
Unit tests for agent/engine.py — the sandboxed subprocess executor.

Tests verify that code executes correctly, that timeouts are enforced,
and that path restrictions prevent access outside the allowed directory.
"""

import os
import sys
from unittest.mock import patch

from agent.engine import execute_sandboxed_code


def _module_is_loaded(name):
    return name in sys.modules


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
        locals_dict, error = execute_sandboxed_code("while True: pass", timeout=2)
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

    def test_standard_file_reads_strip_private_blocks(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        note = allowed / "note.md"
        note.write_text("public <private>secret-canary</private> tail")
        code = (
            "with open('note.md', 'r') as source:\n"
            "    text_read = source.read()\n"
            "from pathlib import Path\n"
            "path_text_read = Path('note.md').read_text()\n"
            "path_bytes_read = Path('note.md').read_bytes()\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict["text_read"] == "public  tail"
        assert locals_dict["path_text_read"] == "public  tail"
        assert locals_dict["path_bytes_read"] == b"public  tail"

    def test_raw_descriptor_read_is_denied(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        (allowed / "note.md").write_text("<private>secret-canary</private>")
        code = (
            "leaked_os = __builtins__['__import__'].__globals__['os']\n"
            "try:\n"
            "    leaked_os.open('note.md', leaked_os.O_RDONLY)\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict["escaped"] is False

    def test_platform_raw_io_module_cannot_read_private_blocks(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        (allowed / "note.md").write_text("<private>platform-canary</private>")
        backend = os.name
        code = (
            f"import {backend}\n"
            f"fd = {backend}.open('note.md', {backend}.O_RDONLY)\n"
            f"raw_content = {backend}.read(fd, 4096)\n"
            f"{backend}.close(fd)\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert locals_dict is not None
        assert f"Import of '{backend}' is denied" in error
        assert "platform-canary" not in repr(locals_dict)

    def test_raw_descriptor_write_preserves_private_file(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        note = allowed / "note.md"
        before = b"before <private>secret-canary</private> after"
        note.write_bytes(before)
        code = (
            "leaked_os = __builtins__['__import__'].__globals__['os']\n"
            "try:\n"
            "    leaked_os.open('note.md', leaked_os.O_WRONLY | leaked_os.O_TRUNC)\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict["escaped"] is False
        assert note.read_bytes() == before

    def test_raw_io_backends_cannot_read_private_blocks(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        (allowed / "note.md").write_text("public <private>secret-canary</private>")
        code = (
            "import_target = None\n"
            "for name in import_candidates:\n"
            "    if module_is_loaded(name):\n"
            "        continue\n"
            "    target_preloaded = module_is_loaded(name)\n"
            "    try:\n"
            "        imported_module = __import__(name)\n"
            "    except ImportError:\n"
            "        continue\n"
            "    import_target = name\n"
            "    break\n"
            "if import_target is None:\n"
            "    raise AssertionError('no unloaded standard-library import succeeded')\n"
            "import_value = imported_module.__name__\n"
            "import io\n"
            "try:\n"
            "    leaked = io.FileIO('note.md', 'r').read()\n"
            "    fileio_escaped = True\n"
            "except PermissionError:\n"
            "    fileio_escaped = False\n"
            "try:\n"
            "    code_bytes = io.open_code('note.md').read()\n"
            "    open_code_escaped = True\n"
            "except PermissionError:\n"
            "    open_code_escaped = False\n"
            "import _io\n"
            "try:\n"
            "    raw_leaked = _io.FileIO('note.md', 'r').read()\n"
            "    raw_fileio_escaped = True\n"
            "except PermissionError:\n"
            "    raw_fileio_escaped = False\n"
            "with _io.open('note.md', 'r') as source:\n"
            "    raw_open_read = source.read()\n"
            "try:\n"
            "    raw_code_bytes = _io.open_code('note.md').read()\n"
            "    raw_open_code_escaped = True\n"
            "except PermissionError:\n"
            "    raw_open_code_escaped = False\n"
            "try:\n"
            "    import importlib\n"
            "    importlib_escaped = True\n"
            "except ImportError:\n"
            "    importlib_escaped = False\n"
            "try:\n"
            "    import agent.engine as engine\n"
            "    loader = engine._bootstrap_external.SourceFileLoader('probe', 'note.md')\n"
            "    loader_read = loader.get_data('note.md')\n"
            "    loader_escaped = True\n"
            "except ImportError:\n"
            "    loader_escaped = False\n"
            "try:\n"
            "    import _frozen_importlib_external as frozen_loader\n"
            "    frozen_probe = frozen_loader.SourceFileLoader('probe', 'note.md')\n"
            "    frozen_read = frozen_probe.get_data('note.md').decode()\n"
            "    frozen_alias_escaped = True\n"
            "except ImportError:\n"
            "    frozen_alias_escaped = False\n"
            "blocked_loader_aliases = []\n"
            "for loader_alias in ('_frozen_importlib', 'zipimport'):\n"
            "    try:\n"
            "        __import__(loader_alias)\n"
            "    except ImportError:\n"
            "        blocked_loader_aliases.append(loader_alias)\n"
            "spoofed_open_code = compile(\n"
            "    \"spoofed_raw = io.open_code('note.md').read()\",\n"
            "    '<frozen importlib._bootstrap_external>',\n"
            "    'exec',\n"
            ")\n"
            "try:\n"
            "    exec(spoofed_open_code)\n"
            "    spoofed_open_code_escaped = True\n"
            "except PermissionError:\n"
            "    spoofed_open_code_escaped = False\n"
            "spoofed_open = compile(\n"
            "    \"with io.open('note.md', 'r') as source:\\n    spoofed_read = source.read()\",\n"
            "    '<frozen importlib._bootstrap_external>',\n"
            "    'exec',\n"
            ")\n"
            "exec(spoofed_open)\n"
        )

        locals_dict, error = execute_sandboxed_code(
            code,
            allowed_path=str(allowed),
            available_functions={
                "module_is_loaded": _module_is_loaded,
                "import_candidates": [
                    "fractions",
                    "calendar",
                    "tomllib",
                    "tarfile",
                    "statistics",
                    "gettext",
                    "plistlib",
                    "tokenize",
                    "weakref",
                    "graphlib",
                    "xml.etree.ElementTree",
                    "pprint",
                    "csv",
                    "bisect",
                    "heapq",
                ],
            },
        )

        assert not error
        assert locals_dict["target_preloaded"] is False
        assert locals_dict["import_value"] == locals_dict["import_target"]
        assert locals_dict["fileio_escaped"] is False
        assert locals_dict["open_code_escaped"] is False
        assert locals_dict["raw_fileio_escaped"] is False
        assert locals_dict["raw_open_read"] == "public "
        assert locals_dict["raw_open_code_escaped"] is False
        assert locals_dict["importlib_escaped"] is False
        assert locals_dict["loader_escaped"] is False
        assert locals_dict["frozen_alias_escaped"] is False
        assert locals_dict["blocked_loader_aliases"] == [
            "_frozen_importlib",
            "zipimport",
        ]
        assert locals_dict["spoofed_open_code_escaped"] is False
        assert locals_dict["spoofed_read"] == "public "
        assert "secret-canary" not in repr(locals_dict)

    def test_raw_io_fileio_write_preserves_private_file(self, tmp_path):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        note = allowed / "note.md"
        before = b"before <private>secret-canary</private> after"
        note.write_bytes(before)
        code = (
            "import _io\n"
            "try:\n"
            "    output = _io.FileIO('note.md', 'w')\n"
            "    output.write(b'overwritten')\n"
            "    output.close()\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict["escaped"] is False
        assert note.read_bytes() == before

    def test_path_replace_preserves_private_destination_and_public_control(
        self, tmp_path
    ):
        allowed = tmp_path / "memory"
        allowed.mkdir()
        private_note = allowed / "private.md"
        private_before = b"public <private>replace-canary</private>"
        private_note.write_bytes(private_before)
        (allowed / "private-source.tmp").write_text("replacement")
        (allowed / "public.md").write_text("old public")
        (allowed / "public-source.tmp").write_text("new public")
        code = (
            "from pathlib import Path\n"
            "try:\n"
            "    Path('private-source.tmp').replace('private.md')\n"
            "    private_replaced = True\n"
            "except PermissionError:\n"
            "    private_replaced = False\n"
            "Path('public-source.tmp').replace('public.md')\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict["private_replaced"] is False
        assert private_note.read_bytes() == private_before
        assert (allowed / "private-source.tmp").read_text() == "replacement"
        assert (allowed / "public.md").read_text() == "new public"


# Must be defined at module level — pickle cannot serialize local functions
def _add(a, b):
    return a + b


class TestAvailableFunctions:
    def test_custom_function_is_callable(self):
        locals_dict, error = execute_sandboxed_code(
            "result = add(3, 4)",
            available_functions={"add": _add},
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
            tool_allowlist={"check_if_file_exists"},
        )
        assert not error
        assert locals_dict["result"] is False

    def test_import_module_requires_an_explicit_tool_allowlist(self, tmp_path):
        with patch("agent.engine.importlib.import_module") as import_module:
            locals_dict, error = execute_sandboxed_code(
                "result = read_file('note.md')",
                allowed_path=str(tmp_path),
                import_module="agent.tools",
            )

        assert locals_dict is None
        assert "tool_allowlist" in error
        import_module.assert_not_called()

    def test_allowlist_does_not_export_raw_vault_tools(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("lifecycle-canary")

        locals_dict, error = execute_sandboxed_code(
            "result = read_file('note.md')",
            allowed_path=str(tmp_path),
            import_module="agent.tools",
            tool_allowlist={"create_dir"},
        )

        assert locals_dict is not None
        assert "NameError" in error
        assert "lifecycle-canary" not in error

    def test_update_file_rejects_private_file_without_mutation(self, tmp_path):
        note = tmp_path / "note.md"
        before = b"before <private>secret-canary</private> after"
        note.write_bytes(before)

        locals_dict, error = execute_sandboxed_code(
            "result = update_file('note.md', 'before', 'changed')",
            allowed_path=str(tmp_path),
            import_module="agent.tools",
            tool_allowlist={"update_file"},
        )

        assert not error
        assert "private" in locals_dict["result"].lower()
        assert note.read_bytes() == before

    def test_update_file_preserves_public_update_behavior(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_bytes(b"before public after")

        locals_dict, error = execute_sandboxed_code(
            "result = update_file('note.md', 'before', 'changed')",
            allowed_path=str(tmp_path),
            import_module="agent.tools",
            tool_allowlist={"update_file"},
        )

        assert not error
        assert locals_dict["result"] is True
        assert note.read_bytes() == b"changed public after"


class TestRestrictedExecutorHardening:
    def test_environment_is_scrubbed(self):
        os.environ["SUPERMEM_TEST_SECRET"] = "do-not-leak"
        locals_dict, error = execute_sandboxed_code(
            "import os\nsecret = os.environ.get('SUPERMEM_TEST_SECRET')"
        )
        assert locals_dict is not None
        assert "Import of 'os' is denied" in error

    def test_denies_network_imports(self):
        locals_dict, error = execute_sandboxed_code("import socket")
        assert locals_dict is not None
        assert "Import of 'socket' is denied" in error

    def test_import_hook_globals_cannot_os_open_outside_allowed_path(self, tmp_path):
        allowed = tmp_path / "vault"
        allowed.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        code = (
            "try:\n"
            "    leaked_os = __builtins__['__import__'].__globals__['os']\n"
            f"    fd = leaked_os.open('{outside}', leaked_os.O_RDONLY)\n"
            "    leaked_os.close(fd)\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict.get("escaped") is False

    def test_import_hook_globals_cannot_symlink_outside_allowed_path(self, tmp_path):
        allowed = tmp_path / "vault"
        allowed.mkdir()
        outside = tmp_path / "outside-link"
        code = (
            "try:\n"
            "    leaked_os = __builtins__['__import__'].__globals__['os']\n"
            f"    leaked_os.symlink('/etc/passwd', '{outside}')\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )

        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))

        assert not error
        assert locals_dict.get("escaped") is False
        assert not outside.exists()

    def test_commonpath_blocks_prefix_escape(self, tmp_path):
        allowed = tmp_path / "vault"
        sibling = tmp_path / "vault-evil"
        allowed.mkdir()
        sibling.mkdir()
        outside = sibling / "secret.txt"
        code = (
            "try:\n"
            f"    open('{outside}', 'w').write('escaped')\n"
            "    escaped = True\n"
            "except PermissionError:\n"
            "    escaped = False\n"
        )
        locals_dict, error = execute_sandboxed_code(code, allowed_path=str(allowed))
        assert locals_dict.get("escaped") is False
        assert not outside.exists()


def test_allow_installs_keeps_install_runner_when_subprocess_blacklisted(monkeypatch):
    import sys
    import types
    import agent.engine as engine

    module_name = "supermem_fake_install_target"
    sys.modules.pop(module_name, None)

    def fake_run(*args, **kwargs):
        sys.modules[module_name] = types.ModuleType(module_name)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    locals_dict, error = engine._run_user_code(
        f"import {module_name}\ninstalled = True",
        allow_installs=True,
        allowed_path="",
        blacklist=["subprocess.run"],
        available_functions={},
    )

    assert error is None
    assert locals_dict["installed"] is True
    sys.modules.pop(module_name, None)
