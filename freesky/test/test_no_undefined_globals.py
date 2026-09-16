"""Catch names deleted out from under their callers.

A block delete in backend.py once took `_stream_semaphore`, `_process_stream_content`
and two more definitions along with the dead code around them. Nothing failed until
a request hit the endpoint in production. This is import-free (backend.py pulls in
reflex) so it runs anywhere.
"""
import ast
import builtins
from pathlib import Path

MODULES = ["backend.py", "free_sky_hybrid.py", "free_sky.py", "multi_service_streamer.py"]


def _undefined(source: str) -> list:
    tree = ast.parse(source)
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            defined.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - defined)


def test_no_undefined_globals():
    for module in MODULES:
        path = Path(__file__).resolve().parents[1] / module
        assert _undefined(path.read_text()) == [], f"{module} references undefined names"


if __name__ == "__main__":
    test_no_undefined_globals()
    print("ok")
