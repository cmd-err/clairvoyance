import ast
import datetime
import functools
import operator
import resource
import sys
import threading
import types
from multiprocessing import get_context
from typing import Any, Callable, Dict, Optional

from RestrictedPython import (
    PrintCollector,
    compile_restricted_exec,
    safe_builtins,
    safe_globals,
)
from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    safer_getattr,
)

from app.core.logger import logger

# =============================================================================
# CUSTOM FUNCTION COMPILATION UTILITIES
# =============================================================================
#
# SECURITY: template `python_code` is author-supplied and is compiled+executed
# under RestrictedPython — a transforming compiler that rewrites attribute and
# subscript access to go through guard functions, so the classic CPython
# sandbox escape (walking `().__class__.__base__.__subclasses__()` to reach
# subprocess.Popen / recover real builtins) is rejected at COMPILE time, not by
# a fragile AST name denylist. `safer_getattr` additionally blocks every
# attribute whose name starts with "_". Template code never runs in this
# process: each call runs in a fresh child with a memory cap that is killed at
# its deadline, so a runaway handler cannot hold this process's GIL or memory.
# shortcut: the child shares the pod's network and service-account token, so a
# RestrictedPython escape would still reach them; move execution to a separate
# no-egress runner (docs/CUSTOM_FUNCTION_SANDBOX.md) before self-serve merchants
# get custom functions. The feature stays gated by ENABLE_CUSTOM_PYTHON_FUNCTIONS.

# fork, not spawn/forkserver: those re-run the parent's main script (run.py, the
# whole app) in every child. The child only runs sandboxed code and is killed.
_FORK = get_context("fork")
_CHILD_MEMORY_BYTES = 128 * 1024 * 1024
_CHILD_SLOTS = threading.BoundedSemaphore(4)

# The datetime MODULE holds module references of its own; handlers only ever
# need its value types, so they get those under the same names.
_SAFE_DATETIME = types.SimpleNamespace(
    date=datetime.date,
    datetime=datetime.datetime,
    time=datetime.time,
    timedelta=datetime.timedelta,
    timezone=datetime.timezone,
    tzinfo=datetime.tzinfo,
    MINYEAR=datetime.MINYEAR,
    MAXYEAR=datetime.MAXYEAR,
    UTC=datetime.timezone.utc,
)

# Value allow-list layered on top of RestrictedPython's own safe_builtins. These
# are safe builtins the legacy sandbox exposed and existing handlers may rely on.
_EXTRA_SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "bool": bool,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "hasattr": hasattr,
    "hash": hash,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "zip": zip,
    "datetime": _SAFE_DATETIME,
}


def _no_module(value: Any) -> Any:
    """Backstop: a module object in sandboxed hands is a door out of it."""
    if isinstance(value, types.ModuleType):
        raise AttributeError("module objects are not available in custom functions")
    return value


def _guarded_getattr(obj: Any, name: str, default: Any = None) -> Any:
    return _no_module(safer_getattr(obj, name, default))


def _guarded_getitem(obj: Any, index: Any) -> Any:
    return _no_module(default_guarded_getitem(obj, index))


class CompilationError(Exception):
    """Raised when custom function compilation fails."""


# Augmented-assignment operators. RestrictedPython rewrites ``n += v`` into
# ``n = _inplacevar_("+=", n, v)`` but ships no default ``_inplacevar_``; without
# one, any ``+=``/``-=``/... in trusted code raises NameError at runtime.
_INPLACE_OPS: Dict[str, Callable[[Any, Any], Any]] = {
    "+=": operator.iadd,
    "-=": operator.isub,
    "*=": operator.imul,
    "/=": operator.itruediv,
    "//=": operator.ifloordiv,
    "%=": operator.imod,
    "**=": operator.ipow,
    "<<=": operator.ilshift,
    ">>=": operator.irshift,
    "&=": operator.iand,
    "|=": operator.ior,
    "^=": operator.ixor,
    "@=": operator.imatmul,
}


def _guarded_inplacevar(op: str, target: Any, value: Any) -> Any:
    """``_inplacevar_`` hook for augmented assignment on a plain-name target.

    RestrictedPython only routes bareword targets through this hook —
    attribute/subscript augmented targets (``obj.x += 1``) are rejected at
    compile time — so this is semantics-identical to the already-permitted
    ``n = n + v`` and opens no new capability.
    """
    func = _INPLACE_OPS.get(op)
    if func is None:
        raise CompilationError(f"Unsupported augmented assignment operator: {op!r}")
    return func(target, value)


def _build_restricted_namespace() -> Dict[str, Any]:
    """Guarded globals for executing compiled custom-function code.

    Beyond the read guards, RestrictedPython's transformer also emits calls to
    ``_write_`` (attribute/subscript assignment), ``_inplacevar_`` (augmented
    assignment), and ``_print_`` (``print``). Supplying RestrictedPython's own
    safe primitives — ``full_write_guard`` (dict/list mutate freely, everything
    else stays write-guarded), ``_guarded_inplacevar``, and ``PrintCollector``
    (captures output in-memory, never touches stdout/files) — lets ordinary
    trusted code run without NameErrors while preserving the sandbox.
    """
    namespace: Dict[str, Any] = dict(safe_globals)
    builtins_ns = dict(safe_builtins)
    builtins_ns.update(_EXTRA_SAFE_BUILTINS)
    namespace["__builtins__"] = builtins_ns
    namespace["_getattr_"] = _guarded_getattr
    namespace["_getitem_"] = _guarded_getitem
    namespace["_getiter_"] = default_guarded_getiter
    namespace["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    namespace["_write_"] = full_write_guard
    namespace["_inplacevar_"] = _guarded_inplacevar
    namespace["_print_"] = PrintCollector
    return namespace


def _child_main(writer: Any, source: str, args: Any, context: Any) -> None:
    """Child process: cap memory before any template code runs, then call handler."""
    if sys.platform == "linux":  # RLIMIT_AS is not enforced on macOS
        with open("/proc/self/statm") as f:
            used = int(f.read().split()[0]) * resource.getpagesize()
        limit = used + _CHILD_MEMORY_BYTES
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    try:
        namespace = _build_restricted_namespace()
        code = compile_restricted_exec(source).code
        assert code is not None  # source was checked at build
        exec(code, namespace)  # noqa: S102
        writer.send((True, namespace["handler"](args, context)))
    except BaseException as e:  # noqa: BLE001 - re-raised in the parent
        writer.send((False, e))


def _run_in_child(source: str, timeout: float, args: Any, context: Any) -> Any:
    """Run one handler call in a fresh child process; kill it at the deadline."""
    if not _CHILD_SLOTS.acquire(blocking=False):
        raise RuntimeError("Too many custom functions running at once")
    try:
        reader, writer = _FORK.Pipe(duplex=False)
        with reader, writer:
            child = _FORK.Process(
                target=_child_main, args=(writer, source, args, context)
            )
            child.start()
            writer.close()  # so a child that dies without answering reads as EOF
            try:
                if not reader.poll(timeout):
                    raise TimeoutError(f"Function timed out after {timeout}s")
                ok, value = reader.recv()
            except EOFError:
                raise RuntimeError("Custom function stopped without a result") from None
            finally:
                child.kill()
                child.join()
    finally:
        _CHILD_SLOTS.release()
    if ok:
        return value
    raise value


def compile_custom_function(
    name: str, source: str, timeout: float = 5
) -> Optional[Callable]:
    """
    Check python_code and return a callable that runs it in a child process.

    The source must define a top-level ``def handler(args, context)``. Nothing
    from the source runs here (see the module security note above); top-level
    code runs in the child on every call.

    Args:
        name: Function name (for logging/debugging)
        source: Python source code string
        timeout: Seconds before the child running a call is killed

    Returns:
        The handler callable, or None if compilation failed (logs warning)
    """
    try:
        result = compile_restricted_exec(source, filename=f"<custom_function:{name}>")
        if result.errors:
            raise CompilationError("; ".join(result.errors))
        if result.code is None:
            raise CompilationError("RestrictedPython produced no code object")

        tree = ast.parse(source)
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
            raise CompilationError("import statements are not allowed")

        handler = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "handler"
            ),
            None,
        )
        if handler is None:
            logger.warning(
                f"[custom function '{name}'] python_code does not define a top-level 'def handler(args, context)'"
            )
            return None

        if len(handler.args.posonlyargs + handler.args.args) < 2 and not (
            handler.args.vararg
        ):
            logger.warning(
                f"[custom function '{name}'] 'handler' must accept at least 2 arguments (args, context)"
            )
            return None

        return functools.partial(_run_in_child, source, timeout)

    except SyntaxError as e:
        logger.warning(f"[custom function '{name}'] syntax error: {e}")
        return None
    except CompilationError as e:
        logger.warning(f"[custom function '{name}'] compilation failed: {e}")
        return None
    except Exception as e:
        logger.warning(
            f"[custom function '{name}'] compilation failed: {e}", exc_info=True
        )
        return None
