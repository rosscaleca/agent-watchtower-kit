"""Static scanner to detect unsafe --json field sets in gh commands.

This module scans Python files for `gh` command-line argument lists that pass
a field set to the --json flag, and reports any field set that names a field
on the deny-list (NESTED_FIELDS).

The scanner uses only the ast module and never imports or executes the files
it scans.
"""

import ast
import os
from collections import namedtuple

# Mapping of field name to reason string for fields that cause nested
# connections when passed to gh's --json flag.
NESTED_FIELDS = {
    "commits": (
        "gh expands commits with implicit authors(first:100) connection, "
        "so GitHub budgets --limit x 100 x 100 nodes and rejects the request "
        "before looking at the data."
    ),
}

# Finding namedtuple for reported issues
Finding = namedtuple("Finding", ("path", "line", "field", "reason"))


def _extract_imports(tree):
    """Extract import information from an AST tree.

    Returns:
        A dict mapping:
          - "aliases": {alias -> original_module_name}
          - "from_imports": {(module, name) -> alias_or_name}

        And for each imported module, a dict of its constants.
    """
    aliases = {}  # alias -> original module name
    from_imports = {}  # (module, name) -> imported_name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                original = alias.name
                asname = alias.asname if alias.asname else alias.name
                aliases[asname] = original
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    name = alias.name
                    asname = alias.asname if alias.asname else name
                    from_imports[(node.module, name)] = asname

    return aliases, from_imports


def _extract_constants(tree):
    """Extract top-level constant definitions from an AST tree.

    Returns:
        A dict mapping name -> value where value is a str, list of str, or tuple of str.
    """
    constants = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            # Only handle simple NAME = value assignments at module level
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                value = _get_constant_value_with_imports(node.value, {}, {}, {}, {})
                # Only record if it's a str or list/tuple of str
                if isinstance(value, str):
                    constants[name] = value
                elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                    constants[name] = value

    return constants


def _resolve_field_set_expression(node, constants, aliases, from_imports, all_module_constants):
    """Resolve a field-set expression node to a string.

    Args:
        node: The AST node for the field set.
        constants: Local module constants.
        aliases: Module aliases (import X as Y).
        from_imports: From-import mappings {(module, name) -> alias}.
        all_module_constants: All modules' constant tables.

    Returns:
        A tuple of (resolved_string, success).
    """
    value = _get_constant_value_with_imports(node, constants, aliases, from_imports, all_module_constants)

    if isinstance(value, str):
        return value, True
    elif isinstance(value, (list, tuple)):
        if all(isinstance(v, str) for v in value):
            return ",".join(value), True
        else:
            return None, False

    return None, False


def _get_constant_value_with_imports(node, constants, aliases, from_imports, all_module_constants):
    """Resolve an AST node to a constant value, considering imports.

    Args:
        node: An AST node.
        constants: Local module constants.
        aliases: Module aliases dict.
        from_imports: From-import mappings.
        all_module_constants: All module constant tables.

    Returns:
        The resolved value or None if unresolvable.
    """
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Name):
        # Look up name in local constants first
        if node.id in constants:
            return constants[node.id]
        # Check from-imports: see if this name was imported from a module
        for (mod_name, const_name), imported_name in from_imports.items():
            if imported_name == node.id:
                # Found the source module for this import
                if mod_name in all_module_constants:
                    const_table = all_module_constants[mod_name]
                    if const_name in const_table["constants"]:
                        return const_table["constants"][const_name]
        return None
    elif isinstance(node, ast.Attribute):
        # M.ATTR - check if M is an imported module or alias
        if isinstance(node.value, ast.Name):
            module_name = node.value.id
            # Check if this is an alias for a module
            original_module = aliases.get(module_name, module_name)

            if original_module in all_module_constants:
                const_table = all_module_constants[original_module]
                if node.attr in const_table["constants"]:
                    return const_table["constants"][node.attr]
        return None
    elif isinstance(node, ast.List):
        values = []
        for elt in node.elts:
            val = _get_constant_value_with_imports(elt, constants, aliases, from_imports, all_module_constants)
            if val is None:
                return None
            values.append(val)
        return tuple(values)  # Return as tuple for consistency
    elif isinstance(node, ast.Tuple):
        values = []
        for elt in node.elts:
            val = _get_constant_value_with_imports(elt, constants, aliases, from_imports, all_module_constants)
            if val is None:
                return None
            values.append(val)
        return tuple(values)
    elif isinstance(node, ast.Call):
        # Check for ",".join(...) or similar
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Constant):
                str_val = node.func.value.value
                if str_val == "," and node.func.attr == "join":
                    # It's a join call, resolve the argument
                    if node.args:
                        return _get_constant_value_with_imports(
                            node.args[0], constants, aliases, from_imports, all_module_constants
                        )
        return None
    return None


def _resolve_str(node, constants, aliases, from_imports, all_module_constants):
    """Resolve a string expression node to its string value.

    Args:
        node: The AST node to resolve.
        constants: Local module constants.
        aliases: Module aliases dict.
        from_imports: From-import mappings.
        all_module_constants: All module constant tables.

    Returns:
        The resolved string, or None if the expression cannot be resolved.
    """
    # ast.Constant with a str value: return the value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    # ast.JoinedStr: concatenate its values
    if isinstance(node, ast.JoinedStr):
        result_parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                # Direct string constant
                result_parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                # FormattedValue: only resolve if conversion is -1 and format_spec is None
                if value.conversion != -1 or value.format_spec is not None:
                    return None
                # Resolve the inner value
                resolved = _resolve_str(value.value, constants, aliases, from_imports, all_module_constants)
                if resolved is None:
                    return None
                result_parts.append(resolved)
            else:
                # Unknown part type in JoinedStr
                return None
        return "".join(result_parts)

    # ast.BinOp with op ast.Add: concatenate resolved parts
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left_resolved = _resolve_str(node.left, constants, aliases, from_imports, all_module_constants)
        right_resolved = _resolve_str(node.right, constants, aliases, from_imports, all_module_constants)
        if left_resolved is None or right_resolved is None:
            return None
        return left_resolved + right_resolved

    # ast.BinOp with op ast.Mod: % formatting
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left_resolved = _resolve_str(node.left, constants, aliases, from_imports, all_module_constants)
        if left_resolved is None:
            return None

        # Right side can be a string or tuple of strings
        right_value = _get_constant_value_with_imports(node.right, constants, aliases, from_imports, all_module_constants)

        if isinstance(right_value, str):
            args = right_value
        elif isinstance(right_value, (tuple, list)) and all(isinstance(v, str) for v in right_value):
            args = tuple(right_value)
        else:
            return None

        try:
            return left_resolved % args
        except (TypeError, ValueError):
            return None

    # "<template>".format(...): positional and keyword arguments must each
    # resolve to a str; *args, **kwargs or anything unresolvable is None.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        template = _resolve_str(node.func.value, constants, aliases, from_imports, all_module_constants)
        if template is None:
            return None
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                return None
            resolved = _resolve_str(arg, constants, aliases, from_imports, all_module_constants)
            if resolved is None:
                return None
            args.append(resolved)
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                return None
            resolved = _resolve_str(keyword.value, constants, aliases, from_imports, all_module_constants)
            if resolved is None:
                return None
            kwargs[keyword.arg] = resolved
        try:
            return template.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError, AttributeError):
            return None

    # Anything else: try _get_constant_value_with_imports
    value = _get_constant_value_with_imports(node, constants, aliases, from_imports, all_module_constants)
    if value is None:
        return None

    if isinstance(value, str):
        return value
    elif isinstance(value, (list, tuple)):
        # Join list/tuple of str with comma
        if all(isinstance(v, str) for v in value):
            return ",".join(value)
        return None

    return None


def _leftmost_str_literal(node):
    """Return the leftmost string literal from an expression.

    Args:
        node: The AST node to inspect.

    Returns:
        The string value of the leftmost literal, or None if not found.
    """
    if isinstance(node, ast.Constant) and isinstance(getattr(node, 'value', None), str):
        return node.value

    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(getattr(first, 'value', None), str):
            return first.value

    if isinstance(node, ast.BinOp):
        return _leftmost_str_literal(node.left)

    # A method called on the literal: "--json={}".format(...) resolves in
    # _resolve_str; any other method leaves it unresolvable, which fails closed.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _leftmost_str_literal(node.func.value)

    return None


def scan(paths):
    """Scan Python files for unsafe --json field sets.

    Args:
        paths: A list of .py file paths to scan.

    Returns:
        A list of Finding objects, sorted by (path, line).
    """
    findings = []
    all_module_constants = {}
    unparseable_files = set()
    parsed_trees = {}  # Cache parsed trees for second pass

    # First pass: build constant tables and parse each file once
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=path)
        except (SyntaxError, OSError) as e:
            # Unparseable file is a finding
            reason = f"unparseable: {type(e).__name__} — the guard cannot vouch for a file it cannot read"
            findings.append(Finding(path=path, line=1, field=None, reason=reason))
            unparseable_files.add(path)
            continue

        module_name = os.path.basename(path).replace(".py", "")
        constants = _extract_constants(tree)
        aliases, from_imports = _extract_imports(tree)

        all_module_constants[module_name] = {
            "constants": constants,
            "aliases": aliases,
            "from_imports": from_imports,
        }
        parsed_trees[path] = tree

    # Second pass: scan for issues (reuse parsed trees)
    for path in paths:
        if path in unparseable_files:
            continue

        module_name = os.path.basename(path).replace(".py", "")
        constants = all_module_constants[module_name]["constants"]
        aliases = all_module_constants[module_name]["aliases"]
        from_imports = all_module_constants[module_name]["from_imports"]

        findings.extend(_scan_file(
            parsed_trees[path], path, constants, aliases, from_imports, all_module_constants
        ))

    # Sort by (path, line)
    findings.sort(key=lambda f: (f.path, f.line))
    return findings


def _scan_file(tree, path, constants, aliases, from_imports, all_module_constants):
    """Scan a single file for issues.

    Returns:
        A list of Finding objects.
    """
    findings = []

    # Track which --json constants were inside a list/tuple
    json_in_list_ids = set()

    # Build parent map for finding maximal nodes
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    def _is_maximal(node):
        """Check if a node is maximal (its parent is not BinOp, JoinedStr, or FormattedValue)."""
        parent = parents.get(node)
        if parent is None:
            return True
        # ast.Attribute: the literal a method is called on ("--json={}".format)
        # belongs to the enclosing Call, which is the maximal expression.
        return not isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.Attribute))

    # First, walk List and Tuple nodes to record id() of --json elements
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elements = node.elts
            for i, elem in enumerate(elements):
                if isinstance(elem, ast.Constant) and elem.value == "--json" and i + 1 < len(elements):
                    # Only a flag WITH a next element gets checked below. A flag that ends
                    # its list (["gh", ..., "--json"] + [fields]) pairs with nothing we can
                    # see, so it must fall through to the bare-flag finding.
                    json_in_list_ids.add(id(elem))
                elif isinstance(elem, ast.Constant) and isinstance(elem.value, str) and elem.value.startswith("--json="):
                    # Record this element's id since it was checked
                    json_in_list_ids.add(id(elem))

    # Second walk: handle --json and --json= expressions using the whole-tree walk
    for node in ast.walk(tree):
        # Only process MAXIMAL nodes (not nested inside BinOp, JoinedStr, or FormattedValue)
        # that are ast.Constant, ast.JoinedStr, or ast.BinOp
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
            # Check if this is a MAXIMAL node
            if not _is_maximal(node):
                continue

            leftmost = _leftmost_str_literal(node)

            if leftmost is not None and leftmost.startswith("--json="):
                # Resolve the full expression
                resolved = _resolve_str(node, constants, aliases, from_imports, all_module_constants)

                if resolved is None:
                    # Unresolvable expression
                    reason = f"unresolvable: --json= value could not be resolved to a string"
                    findings.append(Finding(path=path, line=node.lineno, field=None, reason=reason))
                else:
                    # Extract field set after --json=
                    _check_fields(path, node.lineno, resolved[len("--json="):], findings)
            elif leftmost == "--json":
                # This is a bare --json outside any list/tuple
                if id(node) not in json_in_list_ids:
                    reason = f"unresolvable: --json appears outside an argv list literal, so the guard cannot see its field set"
                    findings.append(Finding(path=path, line=node.lineno, field=None, reason=reason))

    # Third walk: handle --json (bare flag) followed by a next element inside List/Tuple nodes
    # The whole-tree walk now handles ALL --json= cases, so we only need to handle bare --json here
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elements = node.elts
            for i, elem in enumerate(elements):
                elem_value = None
                elem_line = getattr(elem, "lineno", 1)

                if isinstance(elem, ast.Constant):
                    elem_value = elem.value

                if elem_value == "--json":
                    # Next element should be the field set
                    if i + 1 < len(elements):
                        field_set_node = elements[i + 1]
                        field_set_line = getattr(field_set_node, "lineno", elem_line)

                        resolved_str, success = _resolve_field_set_expression(
                            field_set_node, constants, aliases, from_imports, all_module_constants
                        )

                        if not success:
                            # Unresolvable expression
                            reason = f"unresolvable: field set must be a literal or a module-level constant so the guard can read it"
                            findings.append(Finding(path=path, line=field_set_line, field=None, reason=reason))
                        else:
                            _check_fields(path, field_set_line, resolved_str, findings)

    return findings


def _check_fields(path, line, field_set_str, findings):
    """Check field set string for denied fields.

    Args:
        path: The file path.
        line: The line number of the field set expression.
        field_set_str: The resolved field set string.
        findings: List to append findings to.
    """
    # Split on comma and strip whitespace
    fields = [f.strip() for f in field_set_str.split(",")]

    for field in fields:
        if field in NESTED_FIELDS:
            findings.append(Finding(
                path=path,
                line=line,
                field=field,
                reason=NESTED_FIELDS[field]
            ))


def describe(finding):
    """Return a one-line description of a finding.

    Args:
        finding: A Finding object.

    Returns:
        A string like "<path>:<line>: <field> <reason>".
    """
    field_part = f"{finding.field} " if finding.field is not None else ""
    return f"{finding.path}:{finding.line}: {field_part}{finding.reason}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        findings = scan(sys.argv[1:])
        for f in findings:
            print(describe(f))
    else:
        print("Usage: python json_field_guard.py <file1.py> [file2.py ...]")
