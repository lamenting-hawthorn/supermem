import builtins
import _io
import importlib
import importlib._bootstrap_external as _bootstrap_external
import inspect
import io
import logging
import os
import sys
import traceback
import pickle
import subprocess
import base64
from collections.abc import Collection
from typing import Any

from agent.settings import SANDBOX_TIMEOUT
from supermem.privacy.filter import PrivacyFilter

# Configure a logger for the restricted local executor.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_BLACKLIST = [
    "os.system",
    "os.popen",
    "subprocess.Popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
]
DENIED_IMPORT_ROOTS = {
    "agent",
    "os",
    "posix",
    "nt",
    "sys",
    "socket",
    "subprocess",
    "http",
    "importlib",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "urllib",
    "requests",
    "zipimport",
}
_IMPORTLIB_FILE_LOADER_CODES = frozenset(
    {_bootstrap_external.FileLoader.get_data.__code__}
)


def _is_within_path(path: str, root: str) -> bool:
    """Return True when path resolves inside root using path-aware comparison."""
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(root)]
        ) == os.path.realpath(root)
    except ValueError:
        return False


def _run_user_code(
    code: str,
    allow_installs: bool,
    allowed_path: str | None,
    blacklist: list[str],
    available_functions: dict[str, Any],
    log: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Execute code under restricted conditions and return locals and an error.

    This is not hostile-code isolation. The explicit read boundary below prevents
    ordinary model-produced file APIs from returning ``<private>`` blocks.
    """
    orig_open = builtins.open
    orig_io_open = io.open
    orig_raw_io_open = _io.open
    orig_import = builtins.__import__
    orig_remove = os.remove
    orig_rename = os.rename
    original_attrs: list[tuple[object, str, object]] = []

    def restore_state() -> None:
        for obj, attr_name, value in reversed(original_attrs):
            try:
                setattr(obj, attr_name, value)
            except Exception:
                pass
        builtins.open = orig_open
        io.open = orig_io_open
        builtins.__import__ = orig_import
        os.remove = orig_remove
        os.rename = orig_rename

    try:
        # Optional: apply working directory and file access restriction
        if allowed_path:
            allowed = os.path.abspath(allowed_path)
            try:
                os.chdir(allowed)  # Change working dir to the allowed_path
            except Exception as e:
                # If we cannot chdir, log but continue (the open wrapper will still enforce path)
                logger.warning(
                    "Could not change working directory to %s: %s", allowed, e
                )

            def _read_mode(args, kwargs) -> str:
                return kwargs.get("mode", args[0] if args else "r")

            def _reject_private_overwrite(full_path: str) -> None:
                if not os.path.isfile(full_path):
                    return
                with orig_open(full_path, "rb") as existing:
                    raw = existing.read()
                text = raw.decode("utf-8", errors="surrogateescape")
                if PrivacyFilter.has_private(text):
                    raise PermissionError(
                        "Restricted executor cannot overwrite a file "
                        "containing private blocks."
                    )

            def _called_by_real_import_loader() -> bool:
                frame = inspect.currentframe()
                for _ in range(3):
                    if frame is None:
                        return False
                    frame = frame.f_back
                    if (
                        frame is not None
                        and frame.f_code in _IMPORTLIB_FILE_LOADER_CODES
                    ):
                        return True
                return False

            def _restricted_open(original, *, permit_import_loader: bool = False):
                def secure_open(file, *args, **kwargs):
                    """Restrict paths, sanitize reads, and preserve private files."""
                    if permit_import_loader and _called_by_real_import_loader():
                        return original(file, *args, **kwargs)
                    if isinstance(file, int):
                        raise PermissionError(
                            "Raw file descriptors are denied by restricted executor."
                        )
                    full_path = os.path.abspath(os.fspath(file))
                    if not _is_within_path(full_path, allowed):
                        raise PermissionError(
                            f"Access to '{full_path}' is denied by restricted executor."
                        )
                    mode = _read_mode(args, kwargs)
                    if "+" in mode:
                        raise PermissionError(
                            "Read/write file modes are denied by restricted executor."
                        )
                    if "r" not in mode:
                        _reject_private_overwrite(full_path)
                        return original(full_path, *args, **kwargs)
                    with original(full_path, *args, **kwargs) as source:
                        content = source.read()
                    if isinstance(content, bytes):
                        text = content.decode("utf-8", errors="surrogateescape")
                        stream = io.BytesIO(
                            PrivacyFilter.strip_preserving_public_bytes(text).encode(
                                "utf-8", errors="surrogateescape"
                            )
                        )
                        setattr(
                            stream,
                            "_supermem_private_redacted",
                            PrivacyFilter.has_private(text),
                        )
                        return stream
                    stream = io.StringIO(
                        PrivacyFilter.strip_preserving_public_bytes(content)
                    )
                    setattr(
                        stream,
                        "_supermem_private_redacted",
                        PrivacyFilter.has_private(content),
                    )
                    return stream

                return secure_open

            # ``pathlib.Path.read_text/read_bytes`` route through ``io.open``;
            # patch both ordinary public entry points rather than claiming this
            # restricted executor is a general hostile-code sandbox.
            builtins.open = _restricted_open(orig_open)
            original_attrs.append((io, "open", orig_io_open))
            io.open = _restricted_open(orig_io_open)
            original_attrs.append((_io, "open", orig_raw_io_open))
            _io.open = _restricted_open(orig_raw_io_open, permit_import_loader=True)

            def _deny_raw_io_backend(name: str):
                def denied(*args, **kwargs):
                    raise PermissionError(
                        f"Raw {name} is denied by restricted executor."
                    )

                return denied

            def _restricted_open_code(original):
                def secure_open_code(file, *args, **kwargs):
                    if _called_by_real_import_loader():
                        return original(file, *args, **kwargs)
                    raise PermissionError(
                        "Raw open_code is denied by restricted executor."
                    )

                return secure_open_code

            # ``io.FileIO`` and ``io.open_code`` bypass the ordinary ``open``
            # family above. Deny raw byte backends instead of pretending this
            # model-code executor can safely mediate their bytes. The private
            # backend uses exact frozen-loader code objects (not filenames) for
            # its narrowly required import path; ordinary executor calls remain
            # denied or redacted.
            for module, attribute in ((io, "FileIO"), (_io, "FileIO")):
                if hasattr(module, attribute):
                    original_attrs.append(
                        (module, attribute, getattr(module, attribute))
                    )
                    setattr(module, attribute, _deny_raw_io_backend(attribute))
            for module in (io, _io):
                if hasattr(module, "open_code"):
                    original = getattr(module, "open_code")
                    original_attrs.append((module, "open_code", original))
                    setattr(module, "open_code", _restricted_open_code(original))

            # Optionally, restrict other file-related functions (remove, rename, etc.) similarly
            # We'll patch a couple of common ones as an example:

            def secure_remove(path, *args, **kwargs):
                full_path = os.path.abspath(path)
                if not _is_within_path(full_path, allowed):
                    raise PermissionError(
                        f"Removal of '{full_path}' is denied by restricted executor."
                    )
                return orig_remove(path, *args, **kwargs)

            os.remove = secure_remove

            def secure_rename(src, dst, *args, **kwargs):
                full_src = os.path.abspath(src)
                full_dst = os.path.abspath(dst)
                if not _is_within_path(full_src, allowed) or not _is_within_path(
                    full_dst, allowed
                ):
                    raise PermissionError(
                        "Rename operation outside allowed path is denied by restricted executor."
                    )
                return orig_rename(src, dst, *args, **kwargs)

            os.rename = secure_rename

            def _contained_path(path) -> str:
                full_path = os.path.abspath(os.fspath(path))
                if not _is_within_path(full_path, allowed):
                    raise PermissionError(
                        f"Access to '{full_path}' is denied by restricted executor."
                    )
                return full_path

            def _patch_os_path_function(name: str, wrapper_factory) -> None:
                if hasattr(os, name):
                    original = getattr(os, name)
                    original_attrs.append((os, name, original))
                    setattr(os, name, wrapper_factory(original))

            def _single_path_wrapper(original):
                def wrapped(path, *args, **kwargs):
                    return original(_contained_path(path), *args, **kwargs)

                return wrapped

            def _os_open_wrapper(original):
                def wrapped(path, flags, mode=0o777, *, dir_fd=None):
                    if dir_fd is not None:
                        raise PermissionError(
                            "dir_fd based os.open is denied by restricted executor."
                        )
                    if flags & os.O_ACCMODE != os.O_WRONLY:
                        raise PermissionError(
                            "Raw descriptor reads are denied by restricted executor."
                        )
                    full_path = _contained_path(path)
                    _reject_private_overwrite(full_path)
                    return original(full_path, flags, mode)

                return wrapped

            def _rename_like_wrapper(original):
                def wrapped(src, dst, *args, **kwargs):
                    source_path = _contained_path(src)
                    destination_path = _contained_path(dst)
                    if os.path.isdir(source_path) or os.path.isdir(destination_path):
                        raise PermissionError(
                            "Directory rename/link operations are denied by "
                            "restricted executor."
                        )
                    _reject_private_overwrite(source_path)
                    _reject_private_overwrite(destination_path)
                    return original(source_path, destination_path, *args, **kwargs)

                return wrapped

            def _walk_wrapper(original):
                def wrapped(top, *args, **kwargs):
                    return original(_contained_path(top), *args, **kwargs)

                return wrapped

            _patch_os_path_function("open", _os_open_wrapper)
            for function_name in (
                "listdir",
                "scandir",
                "mkdir",
                "makedirs",
                "rmdir",
                "unlink",
                "chmod",
                "lchmod",
                "chown",
                "lchown",
                "utime",
                "truncate",
                "mkfifo",
                "mknod",
                "removedirs",
            ):
                _patch_os_path_function(function_name, _single_path_wrapper)
            for function_name in ("replace", "link", "symlink"):
                _patch_os_path_function(function_name, _rename_like_wrapper)
            _patch_os_path_function("walk", _walk_wrapper)

        install_runner = subprocess.run

        # Apply blacklist restrictions by removing or disabling blacklisted builtins or attributes
        if blacklist:
            for name in blacklist:
                # If the name has a dot, like "os.system", handle module attributes
                if "." in name:
                    mod_name, attr_name = name.split(".", 1)
                    try:
                        mod_obj = importlib.import_module(mod_name)
                    except ImportError:
                        mod_obj = None
                    # If module is imported in sandbox, remove the attribute
                    if mod_obj and hasattr(mod_obj, attr_name):
                        try:
                            original_attrs.append(
                                (mod_obj, attr_name, getattr(mod_obj, attr_name))
                            )
                            setattr(
                                mod_obj, attr_name, None
                            )  # simple way: nullify the attribute
                        except Exception:
                            pass  # if we cannot set it, ignore (might be read-only)
                else:
                    # It's a built-in or global name; remove from builtins if present
                    if name in builtins.__dict__:
                        original_attrs.append((builtins, name, builtins.__dict__[name]))
                        builtins.__dict__[name] = (
                            None  # or we could del, but setting None prevents use
                        )
            # Additionally, we can ensure __builtins__ in the exec env doesn't contain them (handled below in exec)

        def custom_import(name, globals=None, locals=None, fromlist=(), level=0):
            root_name = name.split(".")[0]
            if root_name in DENIED_IMPORT_ROOTS:
                raise ImportError(
                    f"Import of '{root_name}' is denied by restricted executor."
                )
            try:
                return orig_import(name, globals, locals, fromlist, level)
            except ImportError as e:
                if not allow_installs:
                    raise
                pkg = name.split(".")[0]
                logger.info("Restricted executor: attempting to install '%s'", pkg)
                try:
                    install_runner(
                        [sys.executable, "-m", "pip", "install", pkg],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as inst_err:
                    logger.error(
                        "Restricted executor: failed to install package %s: %s",
                        pkg,
                        inst_err,
                    )
                    raise e
                return orig_import(name, globals, locals, fromlist, level)

        builtins.__import__ = custom_import

        # Prepare an isolated execution namespace. We use an empty globals dict with a fresh builtins.
        exec_globals = {"__builtins__": builtins.__dict__}

        # Add any provided functions to the execution environment
        if available_functions:
            exec_globals.update(available_functions)

        exec_locals: dict[str, Any] = {}  # local variables will be collected here

        error_msg: str | None = None
        try:
            exec(code, exec_globals, exec_locals)  # Execute the user's code
        except Exception as e:
            # Catch any exception and format it
            tb = traceback.format_exc()
            error_msg = f"Exception in restricted code:\n{tb}"
            if log:
                logger.error("Sandbox: code raised an exception: %s", e)
        except SystemExit as e:
            # Handle sys.exit calls (which raise SystemExit)
            code_val = e.code if isinstance(e.code, int) or e.code else 0
            if code_val != 0:
                error_msg = f"Restricted code called sys.exit({code_val})"
                if log:
                    logger.warning(
                        "Sandbox: code exited with non-zero status %s", code_val
                    )
            # For sys.exit(0), we treat it as normal termination (no error)

        # Clean up any blacklisted or internal entries in locals
        exec_locals.pop("__builtins__", None)

        # Collect only picklable locals for returning
        safe_locals: dict[str, Any] = {}
        for var, val in exec_locals.items():
            try:
                pickle.dumps(val)  # test picklability
                safe_locals[var] = val
            except Exception:
                safe_locals[var] = repr(val)  # fallback: use string representation

        if log:
            logger.info("Sandbox execution finished")

        restore_state()
        return safe_locals, error_msg

    except Exception as e:
        restore_state()
        # Catch any unhandled exceptions in the worker process
        if log:
            logger.error(
                "Unhandled exception in restricted executor worker: %s",
                traceback.format_exc(),
            )
        return None, f"Sandbox worker error: {str(e)}"


def execute_sandboxed_code(
    code: str,
    timeout: int = SANDBOX_TIMEOUT,
    allow_installs: bool = False,
    requirements_path: str | None = None,
    allowed_path: str | None = None,
    blacklist: list[str] | None = None,
    available_functions: dict[str, Any] | str | None = None,
    import_module: str | None = None,
    tool_allowlist: Collection[str] | None = None,
    log: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Execute model-produced Python in a restricted subprocess.

    Parameters:
        code (str): The Python code to execute.
        timeout (int): Maximum execution time in seconds for the restricted code (default 10 seconds).
        allow_installs (bool): If True, allow installing missing packages via pip (default False).
        requirements_path (str): Path to a requirements.txt file to install before execution.
        allowed_path (str): Directory path that the code is allowed to access for file I/O.
                            File operations outside this path will be blocked. If None, no extra file restrictions are applied.
        blacklist (list): List of names (builtins or module attributes) that are disallowed in the code.
                          If the code uses any of these, it will be prevented or result in an error.
        available_functions (dict): Dictionary of functions to make available in the sandboxed environment.
                                   The keys are the function names, and the values are the function objects.
        import_module (str): Name of a Python module whose explicitly allowlisted
                             functions should be made available in the sandbox.
        tool_allowlist (Collection[str]): Callable names permitted from
                             ``import_module``. Omitting it fails closed rather
                             than exporting every public callable.

    Returns:
        (dict, str): A tuple containing the dictionary of local variables from the executed code (or None on failure),
                     and an error message (str) if an error/exception occurred, or None if execution was successful.
    """
    # Step 1: If package installs are allowed, handle requirements and prepare environment
    if requirements_path:
        if os.path.isfile(requirements_path):
            logger.info(
                "Installing packages from requirements file: %s", requirements_path
            )
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", requirements_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.error(
                    "Failed to install requirements from %s: %s", requirements_path, e
                )
                # If requirements fail to install, we can choose to abort or continue. Here, abort execution.
                return None, f"Failed to install requirements: {e}"
        else:
            logger.error("Requirements file %s not found.", requirements_path)
            return None, f"Requirements file not found: {requirements_path}"

    # If a module name is provided, import only explicitly allowlisted callables.
    if isinstance(available_functions, str) and not import_module:
        import_module = available_functions
        available_functions = None

    if import_module:
        if tool_allowlist is None:
            return (
                None,
                "An explicit tool_allowlist is required when importing executor tools.",
            )
        try:
            module = importlib.import_module(import_module)
            if not isinstance(available_functions, dict):
                available_functions = {}
            for name in sorted(set(tool_allowlist)):
                if name.startswith("_"):
                    return None, f"Tool '{name}' is not eligible for executor export."
                attr = getattr(module, name, None)
                if not callable(attr):
                    return (
                        None,
                        f"Allowlisted tool '{name}' is not a callable in {import_module}.",
                    )
                available_functions[name] = attr
        except ImportError as e:
            logger.error(f"Failed to import module {import_module}: {e}")
            return None, f"Failed to import module {import_module}: {e}"

    # Step 2: Execute the code in a separate Python subprocess
    params = {
        "code": code,
        "allow_installs": allow_installs,
        "allowed_path": allowed_path,
        "blacklist": list(dict.fromkeys(DEFAULT_BLACKLIST + (blacklist or []))),
        "available_functions": available_functions or {},
        "log": log,
    }

    env = {
        "SANDBOX_PARAMS": base64.b64encode(pickle.dumps(params)).decode(),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    try:
        result = subprocess.run(
            [sys.executable, "-m", "agent.engine"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Restricted code exceeded time limit of %d seconds; terminating.", timeout
        )
        return None, f"TimeoutError: Code execution exceeded {timeout} seconds."

    if result.returncode != 0:
        return None, result.stderr.decode().strip()

    try:
        local_vars, error_msg = pickle.loads(result.stdout)
    except Exception as e:
        return None, f"Failed to decode sandbox output: {e}"

    if error_msg is None:
        error_msg = ""

    return local_vars, error_msg


def _subprocess_entry() -> None:
    """Entry point for sandbox subprocess."""
    params_b64 = os.environ.get("SANDBOX_PARAMS")
    if not params_b64:
        sys.exit(1)
    params = pickle.loads(base64.b64decode(params_b64))
    locals_dict, error = _run_user_code(
        params["code"],
        params.get("allow_installs", False),
        params.get("allowed_path"),
        params.get("blacklist", []),
        params.get("available_functions", {}),
        params.get("log", False),
    )
    sys.stdout.buffer.write(pickle.dumps((locals_dict, error)))


if __name__ == "__main__":
    _subprocess_entry()
