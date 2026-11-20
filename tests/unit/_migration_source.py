"""Inspect the SQL statements actually passed through init_db's migration loop."""

import ast


def migration_sql(source: str) -> set[str]:
    tree = ast.parse(source)
    init = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "init_db")
    result = set()
    for loop in ast.walk(init):
        if not isinstance(loop, ast.For) or not isinstance(loop.target, ast.Name):
            continue
        if not isinstance(loop.iter, (ast.List, ast.Tuple)):
            continue
        executes_item = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "exec_driver_sql"
            and len(n.args) == 1
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == loop.target.id
            for statement in loop.body
            for n in ast.walk(statement)
        )
        if executes_item:
            result.update(
                n.value
                for n in loop.iter.elts
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
    return result
