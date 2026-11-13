"""No two things in main.py share a name. Run: python test_names.py

main.py is one long file. A helper added at line 4,700 with the name of one
defined at line 2,300 silently replaces it for the whole program: in 0.5.8 a
new `_q` (column names for SQL) took the place of `_q` (escape a value for a
web address) and every link with a parameter on every page broke. Nothing
fails to compile when that happens, so it is checked here.
"""
import ast
import collections
import io
import warnings

warnings.simplefilter("ignore", SyntaxWarning)
tree = ast.parse(io.open("main.py", encoding="utf-8").read())
seen = collections.defaultdict(list)
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        seen[node.name].append(node.lineno)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            seen[(a.asname or a.name).split(".")[0]].append(node.lineno)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                seen[t.id].append(node.lineno)

# the same module imported twice under the same name is harmless
ALLOWED = {"_re", "urllib"}
twice = {k: v for k, v in seen.items() if len(v) > 1 and k not in ALLOWED}
assert not twice, f"defined more than once in main.py: {twice}"
print(f"ok - {len(seen)} names, none defined twice")
