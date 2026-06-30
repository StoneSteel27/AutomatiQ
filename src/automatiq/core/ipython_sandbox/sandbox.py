import logging
import multiprocessing
import os
import queue
import re
import threading
import time

from .utils import format_output, hard_kill_process, interrupt_process, parse_offset
from .worker import ipython_worker

logger = logging.getLogger(__name__)


class AgentSandbox:
    def __init__(self, working_dir: str = ".", timeout_seconds: int = 3, bin_path: str | None = None):
        self.working_dir = os.path.realpath(working_dir)
        self.timeout = timeout_seconds
        self.bin_path = bin_path
        self.output_file = os.path.join(self.working_dir, ".sandbox_out.log")
        self.history, self.output_cache, self.cell_counter = [], {}, 0
        self.progress_callback = None  # Optional[Callable[[int, int], None]] — (current, total)

        logger.debug(f"Initializing AgentSandbox in {self.working_dir} (Timeout: {self.timeout}s)")
        os.makedirs(self.working_dir, exist_ok=True)

        self.command_queue = None
        self.result_queue = None
        self.interrupt_event = None
        self.process = None

        self._executing = threading.Event()
        self._cancel_flag = threading.Event()
        self._cancel_result: str | None = None
        self._closed = False

        self.start_process()

    def start_process(self) -> None:
        logger.debug("Starting/Restarting IPython worker process...")

        if self.process and self.process.is_alive():
            hard_kill_process(self.process)
            self.process.join(timeout=1)

        if self.command_queue:
            self.command_queue.close()
        if self.result_queue:
            self.result_queue.close()

        self.command_queue = multiprocessing.Queue()
        self.result_queue = multiprocessing.Queue()
        self.interrupt_event = multiprocessing.Event()

        self.process = multiprocessing.Process(
            target=ipython_worker,
            args=(
                self.command_queue,
                self.result_queue,
                self.working_dir,
                self.output_file,
                self.interrupt_event,
                self.bin_path,
            ),
            daemon=True,
        )
        self.process.start()
        logger.debug(f"Worker process started with PID: {self.process.pid}")

        self.command_queue.put(("__PING__", "ping"))
        try:
            self.result_queue.get(timeout=15.0)
            logger.debug("Worker ping successful.")
        except queue.Empty:
            logger.error("Worker failed to respond to initial ping.")

    def handle_magic_commands(self, code: str) -> str | None:
        cmd = code.strip()
        if cmd == "%reset":
            logger.debug("Executing %reset command")
            self.history.clear()
            self.output_cache.clear()
            self.cell_counter = 0
            self.start_process()
            return "[System] Status: RESET SUCCESSFUL\nAll history, variables, and imports cleared."
        if cmd == "%restore":
            logger.debug("Executing %restore command")
            if not self.history:
                return "[System] Status: No history to restore."
            self.start_process()
            self.cell_counter = 0
            self.output_cache.clear()
            total = len(self.history)
            for i, past_code in enumerate(self.history):
                self.execute(past_code, custom_timeout=999999, is_restore=True)
                if self.progress_callback:
                    self.progress_callback(i + 1, total)
            return f"[System] Status: RESTORED\nSuccessfully re-ran {total} previous cells."
        if cmd.startswith("%view_output"):
            parts = cmd.split()
            if len(parts) < 2:
                return "Usage: %view_output Cell_X [--offset Y]"
            target_cell = parts[1]
            if target_cell not in self.output_cache:
                return f"Error: {target_cell} not found in cache."
            offset_line, chunk_offset = 1, 1
            if "--offset" in parts:
                try:
                    offset_line, chunk_offset = parse_offset(parts[parts.index("--offset") + 1])
                except Exception as e:
                    return f"Error parsing offset: {e}"
            return f"[Pager] {target_cell} | Starting at line {offset_line}\n" + format_output(
                self.output_cache[target_cell], target_cell, offset_line, chunk_offset
            )
        return None

    def execute(self, code: str, custom_timeout: int | None = None, is_restore: bool = False) -> str:
        if self._closed:
            return "Error: Sandbox is closed."

        magic_res = self.handle_magic_commands(code)
        if magic_res is not None:
            return magic_res

        self.cell_counter += 1
        cell_id = f"Cell_{self.cell_counter}"
        timeout = custom_timeout if custom_timeout is not None else self.timeout

        logger.debug(f"Executing {cell_id} (Timeout: {timeout}s)")
        if not is_restore:
            logger.debug(f"Code payload:\n{code}")

        if self.process and self.process.is_alive():
            while not self.result_queue.empty():
                try:
                    self.result_queue.get_nowait()
                except Exception:
                    break

        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write("")

        self._cancel_flag.clear()
        self.command_queue.put((cell_id, code))
        self._executing.set()

        status, code_exit, ret_val = "error", 1, ""
        timeout_msg = ""
        fatal_timeout = False

        try:
            start_time = time.monotonic()
            res = None

            while time.monotonic() - start_time < timeout:
                if self._cancel_flag.is_set():
                    break
                try:
                    res = self.result_queue.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue

            if res:
                status, code_exit, ret_val = res["status"], res["exit_code"], res["ret_val"]
                if self._cancel_flag.is_set():
                    status, code_exit, ret_val = "error", 1, "CancelledByUser"
                    timeout_msg = "\n[Execution interrupted by user. State preserved.]"
                    self._cancel_result = "preserved"

            elif self._cancel_flag.is_set():
                try:
                    res = self.result_queue.get(timeout=3.0)
                    status, code_exit, ret_val = "error", 1, "CancelledByUser"
                    timeout_msg = "\n[Execution interrupted by user. State preserved.]"
                    self._cancel_result = "preserved"
                except queue.Empty:
                    logger.debug("Worker failed to acknowledge cancellation interrupt. Hard killing...")
                    status, code_exit, ret_val = "error", 1, "CancelledByUser (Hard Killed)"
                    timeout_msg = "\n[Execution forcefully terminated. State lost.]"
                    self._cancel_result = "lost"
                    self.start_process()

            else:
                logger.warning(f"Soft Timeout ({timeout}s) reached for {cell_id}. Sending interrupt...")
                interrupt_process(self.process, self.interrupt_event)
                try:
                    res = self.result_queue.get(timeout=1.5)
                    status, code_exit, ret_val = "error", res.get("exit_code", 1), res.get("ret_val", "")
                    timeout_msg = "\n[TIMEOUT: Execution interrupted. State preserved.]"
                except queue.Empty:
                    logger.error(f"Hard Timeout reached for {cell_id}. Process unresponsive. Hard killing...")
                    status, code_exit, ret_val = "error", 1, "HardTimeout"
                    fatal_timeout = True
                    timeout_msg = "\n[FATAL TIMEOUT: Execution forcefully terminated. State lost.]"
                    self.start_process()

        except (EOFError, OSError, ValueError):
            status, code_exit, ret_val = "error", 1, "Worker Process Crashed"

        finally:
            self._executing.clear()
            self._cancel_flag.clear()

        try:
            with open(self.output_file, encoding="utf-8", errors="replace") as f:
                out = f.read().strip()
        except Exception as e:
            logger.error(f"Failed to read output file: {e}")
            out = ""

        if ret_val:
            out = out + "\n" + ret_val if out else ret_val
        if timeout_msg:
            out = out + timeout_msg if out else timeout_msg.strip()

        out = re.compile(r"(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]").sub("", out)
        self.output_cache[cell_id] = out

        formatted_out = format_output(out, cell_id) if out else ""
        if status == "success" and not is_restore:
            self.history.append(code)

        header = (
            f"[{cell_id}] Status: Success" if status == "success" else f"[{cell_id}] Status: ERROR (Exit {code_exit})"
        )
        if fatal_timeout:
            header += "\n💡 HINT: Run `%restore` to recover your previous variables and imports."

        return f"{header}\n{formatted_out}" if formatted_out else header

    @property
    def cancel_result(self) -> str | None:
        """Result of the most recent cancel(). None if no cancel in progress/completed.
        Values: None (no cancel), "preserved" (softkill worked), "lost" (hardkill needed).
        """
        return self._cancel_result

    def cancel(self) -> None:
        """Non-blocking cancel — sends soft interrupt to the execution thread."""
        if not self._executing.is_set():
            self._cancel_result = "preserved"
            return

        self._cancel_flag.set()
        logger.debug("Cancel requested — attempting soft interrupt...")

        if not self.process or not self.process.is_alive():
            self._executing.clear()
            self._cancel_result = "lost"
            self.start_process()
            return

        interrupt_process(self.process, self.interrupt_event)

    def close(self) -> None:
        logger.debug("Closing AgentSandbox and cleaning up processes...")
        self._closed = True

        # Stop execution loop if any
        self._cancel_flag.set()

        if self.process and self.process.is_alive():
            hard_kill_process(self.process)
            self.process.join(timeout=1)
        if self.command_queue:
            self.command_queue.close()
        if self.result_queue:
            self.result_queue.close()

        if os.path.exists(self.output_file):
            for _ in range(5):
                try:
                    os.remove(self.output_file)
                    break
                except PermissionError:
                    time.sleep(0.2)
                except Exception:
                    break
        logger.debug("Cleanup complete.")
