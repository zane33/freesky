"""Catch names that are used but never defined.

This exists because two real bugs of exactly this shape shipped: `auth` was
read in the settings page's on_load without ever being bound (every load of
/settings raised NameError), and `config` was used in backend._process_stream_content
while never being imported (every vidembed/fallback playlist rewrite raised
NameError, surfacing as a dead channel).

Neither is caught by an import check or by building the component tree, because
Python only resolves a global when the line actually executes — so the bug hides
until a user hits that exact path.

The check is bytecode-level rather than AST-level, and therefore precise: the
compiler already decided which names are locals and which are globals, so every
LOAD_GLOBAL is genuinely a name Python will look up in module globals and then
builtins at runtime. If it is in neither, that line cannot succeed.

There is no linter in this project's dependencies; this is deliberately a
stdlib-only stand-in for `pyflakes --select=F821`.
"""
import builtins
import dis
import importlib
import types

import pytest

# Every module whose functions should resolve. Deliberately explicit rather than
# a directory walk: freesky/test/ and the unused *_new_architecture module would
# add noise without adding coverage of shipped code.
MODULES = [
    "freesky.backend",
    "freesky.virtual_session",
    "freesky.virtual_channels",
    "freesky.free_sky",
    "freesky.free_sky_hybrid",
    "freesky.users",
    "freesky.app_settings",
    "freesky.channel_prefs",
    "freesky.auth_state",
    "freesky.utils",
    "freesky.pages.settings",
    "freesky.pages.watch",
    "freesky.pages.playlist",
    "freesky.pages.schedule",
    "freesky.pages.auth",
]


def _nested_codes(code):
    """A code object and every code object nested inside it.

    Comprehensions, lambdas and inner functions each compile to their own code
    object, so walking them is what makes the check cover a name used only
    inside a list comprehension in a handler.
    """
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _nested_codes(const)


def _functions(module):
    """Plain functions and methods defined in `module`.

    Reflex wraps event handlers, so a class attribute may be an EventHandler
    rather than a function; `.fn` unwraps those. Attributes are inspected via
    the class __dict__ and matched by isinstance BEFORE any attribute access
    that could hit a Reflex Var descriptor — `getattr(var, "fn", None) or ...`
    raises VarTypeError, because `or` calls __bool__ on the Var it builds.
    """
    seen = set()
    module_file = getattr(module, "__file__", None)

    def emit(obj):
        candidates = []
        if isinstance(obj, types.FunctionType):
            candidates.append(obj)
        else:
            for attr in ("fn", "__func__", "__wrapped__"):
                inner = obj.__dict__.get(attr) if hasattr(obj, "__dict__") else None
                if isinstance(inner, types.FunctionType):
                    candidates.append(inner)
        for fn in candidates:
            code = fn.__code__
            # Skip compiler-generated functions (dataclass __init__/__repr__ are
            # compiled from a synthesised string and reference private helpers
            # that legitimately are not module globals).
            if module_file and code.co_filename != module_file:
                continue
            if code in seen:
                continue
            seen.add(code)
            yield fn

    for obj in list(vars(module).values()):
        if isinstance(obj, types.FunctionType):
            yield from emit(obj)
        elif isinstance(obj, type):
            for attr in list(vars(obj).values()):
                yield from emit(attr)


@pytest.mark.parametrize("modname", MODULES)
def test_module_has_no_undefined_globals(modname):
    module = importlib.import_module(modname)
    namespace = vars(module)

    problems = []
    for fn in _functions(module):
        for code in _nested_codes(fn.__code__):
            for instruction in dis.get_instructions(code):
                if instruction.opname != "LOAD_GLOBAL":
                    continue
                name = instruction.argval
                if name in namespace or hasattr(builtins, name):
                    continue
                problems.append(
                    f"{modname}.{fn.__qualname__} uses undefined name "
                    f"{name!r} (near line {code.co_firstlineno})"
                )

    assert not problems, "Undefined names will raise NameError at runtime:\n" + "\n".join(
        sorted(set(problems))
    )
