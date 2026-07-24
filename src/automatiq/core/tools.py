"""Tool Definition and Validation"""

# ruff: noqa: E501


def _extract_syntax_error_details(code: str) -> tuple[str, int | None] | None:
    """
    Extract detailed syntax error information from code.

    Returns (error_message, line_number) or None if no detailed error can be extracted.
    """
    import ast

    # Try ast.parse first (faster, gives line numbers)
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return (e.msg or "syntax error", e.lineno)


def _format_script_preview(code: str, error_line: int | None, context_lines: int = 2) -> str:
    """
    Format a script preview with line numbers and error highlighting.

    Shows `context_lines` lines before and after the error line.
    """
    lines = code.split("\n")
    if not lines:
        return ""

    # If no error line, show first few lines
    if error_line is None:
        preview_lines = lines[:10]
        return "\n".join(f"  {i + 1}: {line}" for i, line in enumerate(preview_lines))

    # Show context around error line
    start = max(0, error_line - context_lines - 1)
    end = min(len(lines), error_line + context_lines)

    preview_lines = []
    for i in range(start, end):
        line_num = i + 1
        marker = ">>>" if line_num == error_line else "   "
        preview_lines.append(f"{marker} {line_num}: {lines[i]}")

    return "\n".join(preview_lines)


def validate_ipython_script(code: str) -> str:
    """Validate IPython script syntax using IPython's native TransformerManager."""
    stripped = code.strip()
    if not stripped:
        raise ValueError("Script cannot be empty.")

    # Sandbox-handled magics bypass IPython's transformer
    if any(stripped.startswith(m) for m in ("%reset", "%restore", "%view_output")):
        return code

    from IPython.core.inputtransformer2 import TransformerManager

    tm = TransformerManager()
    status, _ = tm.check_complete(code + "\n")

    if status == "invalid":
        # Try to extract detailed syntax error info
        error_info = _extract_syntax_error_details(code)
        if error_info:
            msg, lineno = error_info
            preview = _format_script_preview(code, lineno)
            error_msg = f"Syntax error at line {lineno}: {msg}\n\nScript preview:\n{preview}"
            raise ValueError(error_msg)
        else:
            # Fallback: generic message with preview
            preview = _format_script_preview(code, None)
            error_msg = f"Syntax error: the IPython code is invalid.\n\nScript preview:\n{preview}"
            raise ValueError(error_msg)

    if status == "incomplete":
        raise ValueError("Incomplete code: the cell expects more lines (unclosed block, bracket, or string).")

    # check_complete passed — compile the transformed Python to catch deeper errors
    try:
        compile(tm.transform_cell(code), "<cell>", "exec")
    except SyntaxError as e:
        preview = _format_script_preview(code, e.lineno)
        error_msg = f"Syntax error: {e.msg} at line {e.lineno}\n\nScript preview:\n{preview}"
        raise ValueError(error_msg) from e

    return code


def validate_tool_args(tool_name: str, tool_args: dict) -> str | None:
    """Validate tool arguments and return an error message if invalid."""
    if tool_name == "execute_ipython":
        if "ipython_script" not in tool_args or not isinstance(tool_args["ipython_script"], str):
            return "Missing or invalid 'ipython_script' string."

        try:
            validate_ipython_script(tool_args["ipython_script"])
        except ValueError as e:
            # Prefix with field name for clarity
            return f"ipython_script: {e}"

        if "description" not in tool_args or not isinstance(tool_args["description"], str):
            return "Missing 'description' string. Provide a 5-10 word description."

    elif tool_name == "switch_mode":
        valid_modes = ["reading", "testing", "building"]
        if "target_mode" not in tool_args or tool_args["target_mode"] not in valid_modes:
            actual = tool_args.get("target_mode", "<missing>")
            return f"target_mode: Invalid value '{actual}'. Must be one of: {', '.join(valid_modes)}."
        elif "context" not in tool_args or not isinstance(tool_args["context"], str):
            return "Missing or invalid 'context' string."

    elif tool_name == "final_submit":
        if "final_python_script" not in tool_args or not isinstance(tool_args["final_python_script"], str):
            return "Missing or invalid 'final_python_script' string."

    else:
        return f"Unknown tool name: {tool_name}"

    return None


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_ipython",
            "description": "Execute a python or ipython script in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ipython_script": {
                        "type": "string",
                        "description": """
A single IPython cell to execute. Supports standard Python plus IPython features:

### IPython Features:
* magic commands (`%cd`, `%timeit`, `%%timeit`, `%run`, `%store`, `%macro`, `%edit`, `%prun`),
* shell access via `!` prefix (`!ls`, `var = !cmd`),
* dynamic introspection (`obj?`, `obj??`),
* namespace search with wildcards (`%psearch`, `*pattern*`),
* auto-parentheses (`%autocall`: `sin 3` -> `sin(3)`),
* Jinja style variable expansion in shell commands (`!echo {{myvar}}`). Note: single braces `{}` are treated as literal text.
* To pass literal `{{` or `}}` to the shell, escape them like `{{ "{{" }}` or `{{ "}}" }}`.

### Custom Sandbox Commands:
and custom sandbox commands:
* `%reset` (wipe all variables, imports, and history — fresh kernel),
* `%restore` (re-run all previously successful cells to recover state after a crash or reset),
* `%view_output Cell_N [--offset Y]` (page through long output of a past cell).

### Powerful Shell Tools:
You have access to powerful shell tools via `!` prefix:
* - **gron**: Your primary tool for exploring JSON. Makes JSON greppable. ALWAYS use this first when encountering unknown JSON. Example: `!gron file.json | rg -i 'token'`.
* - **rg**: Ripgrep, a fast line-oriented search tool.
  * Example: `!rg -C 2 'search_term' session_dump/`
  * To navigate large single line files(eg:minified files), use: `!rg -i -o '.{0,200}intersted_string.{0,200}' file.js`
* - **jq**: Command-line JSON processor. Use ONLY when you already know the exact schema of the JSON file and need to extract specific paths. Do NOT use for initial exploration. Example: `!cat file.json | jq -c '.data.items[]'`.

### Execution Rules:
Code must be syntactically complete — no dangling blocks or unclosed brackets.
Note: The shell environment is jailed. There are NO `python` executables or other raw binaries available via `!python`.
You must execute and test your Python logic natively within the IPython cell itself.""",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Clear, concise description of what this command does in 5-10 words. Examples:\n"
                            "Input: ls\nOutput: Lists files in current directory\n\n"
                            "Input: git status\nOutput: Shows working tree status\n\n"
                            "Input: npm install\nOutput: Installs package dependencies\n\n"
                            "Input: mkdir foo\nOutput: Creates directory 'foo'"
                        ),
                    },
                },
                "required": ["ipython_script", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_mode",
            "description": "Switch the agent's current mode (reading, testing, building).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_mode": {
                        "type": "string",
                        "enum": ["reading", "testing", "building"],
                        "description": "The mode you want to switch to.",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "A research memo for yourself in the next mode. Write down what "
                            "you were investigating, observed, confident about, etc."
                        ),
                    },
                },
                "required": ["target_mode", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_submit",
            "description": "Submit the final python script once you have successfully built and verified it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "final_python_script": {
                        "type": "string",
                        "description": "The complete, raw python script you have built and successfully tested.",
                    },
                },
                "required": ["final_python_script"],
            },
        },
    },
]
