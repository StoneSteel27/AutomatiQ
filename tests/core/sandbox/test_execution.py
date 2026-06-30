import os

import pytest

from automatiq.core.ipython_sandbox import AgentSandbox


def test_basic_execution(sandbox: AgentSandbox):
    result = sandbox.execute('print("hello world")')
    assert "Status: Success" in result
    assert "hello world" in result


def test_state_persistence(sandbox: AgentSandbox):
    sandbox.execute("x = 42")
    result = sandbox.execute("print(x)")
    assert "Status: Success" in result
    assert "42" in result


def test_execution_error(sandbox: AgentSandbox):
    result = sandbox.execute("1 / 0")
    assert "Status: ERROR" in result
    assert "ZeroDivisionError" in result


def test_syntax_error(sandbox: AgentSandbox):
    """Test handling of native SyntaxError which occurs before execution"""
    result = sandbox.execute("this is not valid python")
    assert "Status: ERROR" in result
    assert "SyntaxError" in result


def test_magic_reset(sandbox: AgentSandbox):
    sandbox.execute("a = 100")
    sandbox.execute("%reset")
    result = sandbox.execute("print(a)")
    assert "Status: ERROR" in result
    assert "NameError" in result


def test_magic_restore(sandbox: AgentSandbox):
    sandbox.execute("important_var = 'saved'")
    # Simulate a crash that wiped the in-memory kernel state
    sandbox.process.terminate()
    sandbox.process.join()
    sandbox.execute("%restore")
    result = sandbox.execute("print(important_var)")
    assert "Status: Success" in result
    assert "saved" in result


def test_magic_restore_resets_cell_counter(sandbox: AgentSandbox):
    """%restore should reset cell_counter so re-executed cells get original IDs."""
    sandbox.execute("x = 1")  # Cell_1
    sandbox.execute("y = 2")  # Cell_2
    assert sandbox.cell_counter == 2

    # Simulate a crash that wiped the in-memory kernel state
    sandbox.process.terminate()
    sandbox.process.join()

    sandbox.execute("%restore")

    # After restore, cell_counter should be back to 2 (not 4)
    assert sandbox.cell_counter == 2
    # output_cache should have Cell_1 and Cell_2 (not Cell_3/Cell_4)
    assert "Cell_1" in sandbox.output_cache
    assert "Cell_2" in sandbox.output_cache
    assert "Cell_3" not in sandbox.output_cache


def test_shell_command(sandbox: AgentSandbox):
    """Test shell command via ! escapes"""
    # Use echo, should work on both Windows (jailed busybox) and POSIX
    result = sandbox.execute("!echo foo")
    assert "Status: Success" in result
    assert "foo" in result.lower()


def test_shell_command_variable_substitution(sandbox: AgentSandbox):
    """Test shell command variable substitution via Jinja-style {{ }} escapes"""
    sandbox.execute("my_var = 'magic_value'")

    # Test {{ }} substitution
    result = sandbox.execute("!echo {{my_var}}")
    assert "Status: Success" in result
    assert "magic_value" in result.lower()

    # Test nested dict/key access in {{ }}
    sandbox.execute("my_dict = {'key': 'nested_magic'}")
    result = sandbox.execute("!echo {{my_dict['key']}}")
    assert "Status: Success" in result
    assert "nested_magic" in result.lower()

    # Test that single { } are passed through as literal text (not interpolated)
    # This is crucial for awk/rg/regex support.
    result = sandbox.execute("!echo {literal_braces}")
    assert "Status: Success" in result
    assert "{literal_braces}" in result.lower()

    # Test escaping {{ and }} using the {{ '{{' }} trick
    result = sandbox.execute("!echo {{ '{{' }}literal{{ '}}' }}")
    assert "Status: Success" in result
    assert "{{literal}}" in result.lower()

    # Test that $ is passed to the shell (not interpolated by Python)
    # In our jailed environment, $my_var won't be a shell variable, so it should be empty or literal $my_var
    result = sandbox.execute("!echo $my_var")
    assert "Status: Success" in result
    assert "magic_value" not in result.lower()


def test_shell_command_error(sandbox: AgentSandbox):
    """Test shell command that fails"""
    result = sandbox.execute("!some_nonexistent_binary_123")
    # IPython catches the shell error and prints it, but the python cell itself 'succeeded'
    assert "Status: Success" in result
    assert "not found" in result.lower() or "not recognized" in result.lower() or "error" in result.lower()


def test_magic_view_output(sandbox: AgentSandbox):
    # generate many lines
    sandbox.execute("for i in range(150):\n    print(f'Line {i}')")
    # first view (cell 1)
    out1 = sandbox.execute("%view_output Cell_1")
    assert "Line 0" in out1
    assert "Line 100" in out1
    # offset view
    out2 = sandbox.execute("%view_output Cell_1 --offset 100")
    assert "Line 100" in out2
    assert "Line 149" in out2
    assert "Line 0" not in out2


def test_jailed_bin_attachment(sandbox: AgentSandbox):
    """Test if jailed_bin is properly created and attached to the sandbox environment"""
    import os

    from automatiq.core import config

    # Execute python code to read PATH inside the sandbox
    res_path = sandbox.execute("import os; print(os.environ.get('PATH', ''))")
    assert "Status: Success" in res_path
    assert ".jailed_bin" in res_path

    # Check that standard commands like sh/echo/busybox are inside that .jailed_bin directory
    jailed_bin_dir = os.path.join(sandbox.working_dir, ".jailed_bin")
    assert os.path.isdir(jailed_bin_dir)

    # Check standalone binaries exist inside jailed_bin
    ext = ".exe" if os.name == "nt" else ""
    assert os.path.isfile(os.path.join(jailed_bin_dir, f"rg{ext}"))

    # Optional assertions for jq/gron (skipped on CI, run locally)
    if os.path.isfile(os.path.join(config.BIN_DIR, f"jq{ext}")):
        assert os.path.isfile(os.path.join(jailed_bin_dir, f"jq{ext}"))
        res_jq = sandbox.execute("!jq --version", custom_timeout=5)
        assert "Status: Success" in res_jq
        assert "jq-" in res_jq.lower() or "version" in res_jq.lower()

    if os.path.isfile(os.path.join(config.BIN_DIR, f"gron{ext}")):
        assert os.path.isfile(os.path.join(jailed_bin_dir, f"gron{ext}"))
        res_gron = sandbox.execute("!gron --version", custom_timeout=5)
        assert "Status: Success" in res_gron
        assert "gron" in res_gron.lower()

    # If we are on Windows, check busybox commands inside jailed_bin
    if os.name == "nt":
        # Check if busybox commands are linked/copied
        # For example, sh.exe or echo.exe or sed.exe
        assert os.path.isfile(os.path.join(jailed_bin_dir, "sh.exe"))
        assert os.path.isfile(os.path.join(jailed_bin_dir, "echo.exe"))

    # Now verify that rg is properly executable inside the sandbox
    res_rg = sandbox.execute("!rg --version", custom_timeout=5)
    assert "Status: Success" in res_rg
    assert "ripgrep" in res_rg.lower()


def test_rg_recursive_directory_search(sandbox: AgentSandbox):
    """Test that rg can recursively search a directory successfully inside the sandbox"""
    import os

    # Create a subfolder with a file inside sandbox working directory
    subfolder = os.path.join(sandbox.working_dir, "nested_folder")
    os.makedirs(subfolder, exist_ok=True)
    with open(os.path.join(subfolder, "test_file.txt"), "w") as f:
        f.write("target_search_string_12345")

    # Executing rg recursively on the directory
    res = sandbox.execute('!rg "target_search_string_12345" nested_folder')
    assert "Status: Success" in res
    assert "test_file.txt" in res
    assert "target_search_string_12345" in res


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line limit is Windows-specific")
def test_command_limit_handling_on_many_files(sandbox: AgentSandbox):
    """Test that Windows command-line limit is proactively caught and returned as a clear error"""
    import os

    subfolder = os.path.join(sandbox.working_dir, "requests")
    os.makedirs(subfolder, exist_ok=True)

    # Create 1,000 files with long path names to trigger the command-line limit check
    for i in range(1000):
        folder_name = f"dir_bloat_index_{i:04d}_with_extremely_long_description_group"
        folder_path = os.path.join(subfolder, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        file_path = os.path.join(folder_path, "transaction.json")
        with open(file_path, "w") as f:
            f.write('{"metadata": {"url": "dummy_content"}}')

    # Run command that will trigger the 32k character limit
    res = sandbox.execute('!rg "chunk_target_string" requests/*/transaction.json')

    # Verify execution fails gracefully with informative suggestions
    assert "Status: ERROR" in res or "Exit 1" in res
    assert "[Windows Command Limit Error]" in res
    assert "exceeds the Windows CreateProcess limit" in res
    assert "--glob" in res
