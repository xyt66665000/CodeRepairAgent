"""
Base ReAct Agent infrastructure shared by all phase agents.

Extracted from the original repair_agent.py and generalized so each
pipeline phase (decompile-repair, struct-restore, function-signature,
variable-semantic, control-flow) can reuse the same loop, tools, and
context management.
"""

import os
import json
import subprocess
import re
import tiktoken
import ast
import traceback
import datetime
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI

from utils.logger import save_agent_result
from utils.color_print import cprint

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()
API_KEY = os.getenv("API_KEY")
BASE_URL = os.getenv("BASE_URL")
MODEL_NAME = os.getenv("MODEL_NAME")

MAX_CONTEXT_WINDOW = int(os.getenv("MAX_CONTEXT_WINDOW", "128000"))
TOOL_MAX_OUTPUT_TOKENS = int(os.getenv("TOOL_MAX_OUTPUT_TOKENS", "32768"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16384"))

PRINT_MAX_MESSAGE_TOKENS = int(os.getenv("REPAIR_AGENT_PRINT_MAX_MESSAGE_TOKENS", "600"))
PRINT_MAX_OBSERVATION_TOKENS = int(os.getenv("REPAIR_AGENT_PRINT_MAX_OBSERVATION_TOKENS", "600"))
MAX_READ_CODE_SLICE_LINES = int(os.getenv("MAX_READ_CODE_SLICE_LINES", "500"))
MAX_PER_HISTORY_MESSAGE_TOKENS = int(os.getenv("MAX_PER_HISTORY_MESSAGE_TOKENS", "4096"))

DEFAULT_RECENT_STEPS_COUNT = int(os.getenv("REPAIR_AGENT_RECENT_STEPS_COUNT", "9"))
DEFAULT_RECENT_STEPS_OVERFLOW_COUNT = int(os.getenv("REPAIR_AGENT_RECENT_STEPS_OVERFLOW_COUNT", "4"))
DEFAULT_SUMMARY_MAX_LINES = int(os.getenv("REPAIR_AGENT_SUMMARY_MAX_LINES", "200"))
DEFAULT_SUMMARY_RENDER_LINES = int(os.getenv("REPAIR_AGENT_SUMMARY_RENDER_LINES", "40"))
DEFAULT_SUMMARY_KEEP_ON_OVERFLOW = int(os.getenv("REPAIR_AGENT_SUMMARY_KEEP_ON_OVERFLOW", "20"))

enc = tiktoken.encoding_for_model("gpt-4o")

# Strict compile command (from the repair-full-pipeline skill)
DEFAULT_COMPILE_CMD = (
    "gcc -c -Werror=implicit-function-declaration -Werror=implicit-int "
    "-Werror=incompatible-pointer-types -Werror=int-conversion "
    "-Werror=return-type -fno-builtin -fmax-errors=0 -I."
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def clear_quote(input: str) -> str:
    return re.sub(r"^[`\"'\s]+|[`\"'\s]+$", "", input)


def truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    token_ids = enc.encode(text)
    if len(token_ids) <= max_tokens:
        return text
    return enc.decode(token_ids[:max_tokens])


def token_len(text: str) -> int:
    return len(enc.encode(text or ""))


def _fmt_token_usage(used: int, limit: int) -> str:
    if limit <= 0:
        return f"{used}/?"
    pct = (used / limit) * 100
    used_k = used / 1000.0
    limit_k = limit / 1000.0
    return f"{used}/{limit} ({used_k:.1f}k/{limit_k:.0f}k, {pct:.2f}%)"


def _safe_preview(text: str, max_tokens: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    preview = truncate_text_by_tokens(text, max_tokens)
    if token_len(preview) < token_len(text):
        return preview.rstrip() + "\n...[truncated]"
    return preview


# ---------------------------------------------------------------------------
# GCC error parsing
# ---------------------------------------------------------------------------


def parse_gcc_error_lines(lines: List[str]) -> List[dict]:
    """Parse GCC error output lines into structured records."""
    errors: List[dict] = []
    current_error: Optional[dict] = None
    caret_line_re = re.compile(r"^\s*\|?\s*\^")

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")

        error_match = re.match(
            r"^(.+?):(\d+):(\d+):\s*(error|warning|note):\s*(.+)$", line
        )

        if error_match:
            if current_error:
                errors.append(current_error)

            file_path = error_match.group(1)
            line_num = int(error_match.group(2))
            col_num = int(error_match.group(3))
            error_type = error_match.group(4)
            message = error_match.group(5)

            code_line = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].rstrip("\n")
                if not caret_line_re.match(next_line) and not re.match(
                    r"^.+?:\d+:\d+:", next_line
                ):
                    code_line = next_line
                    i += 1
                else:
                    j = i + 1
                    while j < len(lines) and j < i + 5:
                        temp_line = lines[j].rstrip("\n")
                        if caret_line_re.match(temp_line):
                            j += 1
                            continue
                        if re.match(r"^.+?:\d+:\d+:", temp_line):
                            break
                        code_line = temp_line
                        i = j
                        break
                        j += 1

            current_error = {
                "file": file_path,
                "line": line_num,
                "col": col_num,
                "type": error_type,
                "message": message,
                "code": code_line,
            }
        elif current_error and caret_line_re.match(line):
            i += 1
            continue
        elif current_error and line:
            current_error["message"] += " " + line.strip()

        i += 1

    if current_error:
        errors.append(current_error)

    return errors


def format_gcc_error(error: dict) -> str:
    prefix = f"{error['file']}:{error['line']}:{error['col']}: {error['type']}: {error['message']}"
    code = error.get("code")
    if code:
        return f"{prefix}\n    {code}"
    return prefix


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    func: Callable[[str], str]


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def build_common_tools(
    base_dir: str, compile_cmd: str = DEFAULT_COMPILE_CMD
) -> Dict[str, ToolSpec]:
    """Build the standard tool set used by all phase agents."""

    def terminal(cmd: str) -> str:
        cmd = clear_quote(cmd)
        response = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            text=True,
            shell=True,
            errors="ignore",
            cwd=base_dir,
        )
        output = f"stdout:\n{response.stdout.strip()}\n\nstderr:\n{response.stderr.strip()}\n"
        token_ids = enc.encode(output)
        if len(token_ids) <= TOOL_MAX_OUTPUT_TOKENS:
            return output

        truncated = enc.decode(token_ids[:TOOL_MAX_OUTPUT_TOKENS])
        return (
            truncated
            + "\n\n[Output truncated due to token limit. "
            "You may re-run the command with grep/head/tail to retrieve specific information.]"
        )

    def read_code_slice(input: str) -> str:
        text = clear_quote(input).strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload = ast.literal_eval(text)
            except Exception as e:
                return f"invalid input, expect JSON/dict with filepath/line/context: {e}"

        for key in ("filepath", "line"):
            if key not in payload:
                return f"missing required field `{key}`"

        relpath = str(payload["filepath"]).strip()
        try:
            target_line = int(payload["line"])
        except (TypeError, ValueError):
            return "line must be an integer (1-based)"

        try:
            ctx = int(payload.get("context", 5))
        except (TypeError, ValueError):
            return "context must be an integer"

        abs_path = os.path.join(base_dir, relpath)
        if not os.path.exists(abs_path):
            return f"file not found: {abs_path}"

        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as fp:
                lines = fp.readlines()
        except Exception as e:
            return f"unable to read file '{abs_path}': {e}"

        if target_line < 1 or target_line > len(lines):
            return f"line {target_line} out of range (file has {len(lines)} lines)"

        start = max(1, target_line - ctx)
        end = min(len(lines), target_line + ctx)

        slice_len = end - start + 1
        if slice_len > MAX_READ_CODE_SLICE_LINES:
            return (
                f"[slice too large: {relpath}:{start}-{end} ({slice_len} lines)]\n"
                f"Please narrow the context or re-read specific regions."
            )

        snippet_lines = []
        for idx in range(start, end + 1):
            snippet_lines.append(f"{idx}: {lines[idx - 1].rstrip()}")

        snippet = "\n".join(snippet_lines)
        header = f"[slice {relpath}:{start}-{end} (context={ctx})]"
        return f"{header}\n{snippet}"

    def patch_apply(input: str) -> str:
        text = clear_quote(input).strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload = ast.literal_eval(text)
            except Exception as e:
                return f"invalid input, expect JSON/dict: {e}"

        for key in ("filepath", "action", "start"):
            if key not in payload:
                return f"missing required field `{key}`"

        action = str(payload.get("action", "")).lower()
        if action not in ("add", "delete", "update"):
            return "action must be one of: add, delete, update"

        relpath = str(payload["filepath"]).strip()
        try:
            start = int(payload["start"])
        except (TypeError, ValueError):
            return "start must be an integer (1-based)"

        try:
            end = int(payload.get("end", start))
        except (TypeError, ValueError):
            return "end must be an integer (1-based)"

        content = payload.get("content", "")
        needs_content = action in ("add", "update")
        if needs_content and (content is None or content == ""):
            return "content is required for add/update actions"

        abs_path = os.path.join(base_dir, relpath)
        file_exists = os.path.exists(abs_path)

        lines: List[str] = []
        if file_exists:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as fp:
                    lines = fp.readlines()
            except Exception as e:
                return f"unable to read file '{abs_path}': {e}"
        else:
            if action != "add":
                return f"file not found for action {action}: {abs_path}"

        total_lines = len(lines)

        if start < 1:
            return "start must be >= 1"
        if action in ("delete", "update"):
            if end < start:
                return "end must be >= start"
            if total_lines == 0:
                return f"file is empty, cannot {action}"
            if start > total_lines:
                return f"start {start} out of range (file has {total_lines} lines)"
            if end > total_lines:
                return f"end {end} out of range (file has {total_lines} lines)"

        new_lines: List[str] = []
        if needs_content:
            for line in str(content).splitlines(True):
                new_lines.append(line if line.endswith("\n") else (line + "\n"))

        if action == "add":
            insert_at = max(0, min(start - 1, total_lines))
            updated = lines[:insert_at] + new_lines + lines[insert_at:]
        elif action == "delete":
            updated = lines[: start - 1] + lines[end:]
        elif action == "update":
            updated = lines[: start - 1] + new_lines + lines[end:]
        else:
            return f"unsupported action: {action}"

        parent_dir = os.path.dirname(abs_path)
        try:
            os.makedirs(parent_dir, exist_ok=True)
        except Exception as e:
            return f"failed to create directory '{parent_dir}': {e}"

        try:
            with open(abs_path, "w", encoding="utf-8") as fw:
                fw.writelines(updated)
        except (IOError, OSError) as e:
            return f"unable to write file '{abs_path}': {e}"

        return (
            f"applied {action} to {relpath}: start={start}, end={end}, "
            f"new_lines={len(new_lines) if needs_content else 0}, old_lines={total_lines}, new_total={len(updated)}"
        )

    def parse_gcc_errors(input_str: str) -> str:
        cleaned = clear_quote(input_str).strip()
        if not cleaned:
            return 'Please provide a C file, e.g. {"file": "1.c"}.'

        payload: dict = {}
        if cleaned.startswith("{"):
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError:
                try:
                    payload = ast.literal_eval(cleaned)
                except Exception as exc:
                    return f"Failed to parse JSON/dict input: {exc}"

        if not payload:
            tokens = cleaned.split()
            if tokens:
                payload["file"] = tokens[0]
                idx = 1
                while idx < len(tokens):
                    flag = tokens[idx]
                    if flag == "-l" and idx + 1 < len(tokens):
                        payload["line"] = tokens[idx + 1]
                        idx += 2
                        continue
                    if flag == "-n" and idx + 1 < len(tokens):
                        payload["limit"] = tokens[idx + 1]
                        idx += 2
                        continue
                    idx += 1

        c_file = payload.get("file") or payload.get("filepath")
        if not c_file:
            return 'Missing "file" path. Provide input like {"file": "1.c"}.'

        abs_c_file = os.path.join(base_dir, c_file)
        if not os.path.exists(abs_c_file):
            return f"C file not found: {abs_c_file}"

        try:
            compile_proc = subprocess.run(
                ["gcc", "-c", "-w", "-fmax-errors=0", "-I.", c_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                text=True,
                errors="ignore",
                cwd=base_dir,
            )
        except Exception as exc:
            return f"Failed to run gcc: {exc}"

        compiler_output = f"{compile_proc.stderr}\n{compile_proc.stdout}"
        lines = compiler_output.splitlines()

        try:
            errors = parse_gcc_error_lines(lines)
        except Exception as exc:
            combined = compiler_output.strip() or "No compiler output."
            return f"Failed to parse GCC errors: {exc}\nCompiler output:\n{combined}"

        line_filter = payload.get("line")
        if line_filter is not None:
            try:
                target_line = int(str(line_filter).strip())
            except (ValueError, TypeError):
                return "Line filter must be an integer."
            errors = [err for err in errors if err["line"] == target_line]
            if not errors:
                return f"No errors found at line {target_line}."

        limit = payload.get("limit") or payload.get("n")
        if limit is not None:
            try:
                limit_val = int(str(limit).strip())
            except (ValueError, TypeError):
                return "Limit must be an integer."
            if limit_val <= 0:
                return "Limit must be greater than zero."
            errors = errors[:limit_val]

        if not errors:
            if compile_proc.returncode == 0:
                return "Compilation succeeded, no errors."
            combined = compiler_output.strip() or "No compiler output."
            return f"No structured errors found.\nCompiler output:\n{combined}"

        result = "\n\n".join(format_gcc_error(err) for err in errors)
        return truncate_text_by_tokens(result, TOOL_MAX_OUTPUT_TOKENS)

    return {
        "Terminal": ToolSpec(
            name="Terminal",
            func=terminal,
            description=(
                "Execute a shell command in the target directory.\n"
                "Input: a command string.\n"
                "Output: stdout/stderr (may be truncated)."
            ),
        ),
        "Read Code Slice": ToolSpec(
            name="Read Code Slice",
            func=read_code_slice,
            description=(
                "Read a slice of a file around a 1-based line number.\n"
                'Input JSON/dict: {"filepath": "...", "line": 123, "context": 8}.\n'
                "Output: line-numbered slice (may be truncated)."
            ),
        ),
        "Patch Apply": ToolSpec(
            name="Patch Apply",
            func=patch_apply,
            description=(
                "Apply a line-based patch to a file.\n"
                'Input JSON/dict: {"filepath":"...", "action":"add|delete|update", "start":1, "end":1, "content":"..."}.\n'
                "Output: a short status string."
            ),
        ),
        "Parse GCC Errors": ToolSpec(
            name="Parse GCC Errors",
            func=parse_gcc_errors,
            description=(
                "Run `gcc -c -w -fmax-errors=0` on a C file and return formatted diagnostics.\n"
                'Input JSON/dict: {"file":"1.c", "line": 12, "limit": 5} or CLI-like "1.c -l 12 -n 5".\n'
                "Output: formatted errors or success message (may be truncated)."
            ),
        ),
    }


def _format_tool_catalog(tools: Dict[str, ToolSpec]) -> str:
    return "\n".join(f"- {name}: {spec.description}" for name, spec in tools.items())


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _best_effort_json_extract(text: str) -> Optional[dict]:
    text = text.strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _parse_react_response(text: str) -> Tuple[str, str, Any, Optional[str]]:
    """
    Parse either a strict JSON response or a ReAct-style response.

    Returns: (thought, action, action_input, final_answer)
    """
    payload = _best_effort_json_extract(text)
    if payload is not None and isinstance(payload, dict):
        thought = str(payload.get("thought", "")).strip()
        action = str(payload.get("action", "")).strip()
        action_input = payload.get("action_input", "")
        final_answer = payload.get("final")
        if final_answer is not None:
            final_answer = str(final_answer)
        return thought, action, action_input, final_answer

    thought = ""
    action = ""
    action_input_lines: List[str] = []
    final_lines: List[str] = []
    mode: Optional[str] = None

    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("thought:"):
            thought = stripped.split(":", 1)[1].strip()
            mode = None
            continue
        if lower.startswith("action:"):
            action = stripped.split(":", 1)[1].strip()
            mode = "action_input"
            continue
        if lower.startswith("action input:"):
            action_input_lines.append(stripped.split(":", 1)[1].lstrip())
            mode = "action_input"
            continue
        if lower.startswith("final:"):
            final_lines.append(stripped.split(":", 1)[1].lstrip())
            mode = "final"
            continue
        if mode == "action_input":
            action_input_lines.append(line)
        elif mode == "final":
            final_lines.append(line)

    action_input = "\n".join(action_input_lines).strip()
    final_answer = "\n".join(final_lines).strip() if final_lines else None
    return thought.strip(), action.strip(), action_input, final_answer


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def call_llm(client: OpenAI, messages: List[Dict[str, str]], max_tokens: int) -> str:
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _sanitize_log_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except TypeError:
            return str(value)
    return str(value)


def _status_from_output(output: str) -> str:
    if not output:
        return "failure"
    return "success" if "compilation succeeded" in output.lower() else "failure"


# ---------------------------------------------------------------------------
# Async JSONL Logger
# ---------------------------------------------------------------------------


class AsyncJsonlLogger:
    def __init__(self, log_path: str):
        self.log_path = log_path
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def log(self, record: Dict[str, Any]) -> None:
        if "timestamp" not in record:
            record["timestamp"] = datetime.datetime.now().isoformat()
        self._queue.put(record)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()

    def _worker(self) -> None:
        log_dir = os.path.dirname(self.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                while True:
                    item = self._queue.get()
                    if item is None:
                        break
                    try:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                        f.flush()
                    except Exception:
                        continue
        except Exception:
            while True:
                item = self._queue.get()
                if item is None:
                    break


# ---------------------------------------------------------------------------
# Base ReAct Agent
# ---------------------------------------------------------------------------


class BaseReActAgent:
    """ReAct-style agent for iterative code repair tasks.

    All pipeline phase agents use this class, configured with a
    phase-specific system prompt and task description.
    """

    def __init__(
        self,
        client: OpenAI,
        tools: Dict[str, ToolSpec],
        base_dir: str,
        system_prompt: str,
        compile_cmd: str = DEFAULT_COMPILE_CMD,
        max_context_tokens: int = MAX_CONTEXT_WINDOW,
        max_iterations: int = 250,
        max_recent_steps_count: int = DEFAULT_RECENT_STEPS_COUNT,
        max_recent_steps_overflow_count: int = DEFAULT_RECENT_STEPS_OVERFLOW_COUNT,
        summary_max_lines: int = DEFAULT_SUMMARY_MAX_LINES,
        summary_render_lines: int = DEFAULT_SUMMARY_RENDER_LINES,
        summary_keep_on_overflow: int = DEFAULT_SUMMARY_KEEP_ON_OVERFLOW,
        step_logger: Optional[AsyncJsonlLogger] = None,
        verbose: bool = True,
    ):
        self.client = client
        self.tools = tools
        self.base_dir = base_dir
        self.system_prompt = system_prompt
        self.compile_cmd = compile_cmd
        self.max_context_tokens = max_context_tokens
        self.max_iterations = max_iterations
        self.max_recent_steps_count = max(1, int(max_recent_steps_count))
        self.max_recent_steps_overflow_count = max(1, int(max_recent_steps_overflow_count))
        if self.max_recent_steps_overflow_count > self.max_recent_steps_count:
            self.max_recent_steps_overflow_count = self.max_recent_steps_count
        self.summary_max_lines = max(1, int(summary_max_lines))
        self.summary_render_lines = max(1, int(summary_render_lines))
        self.summary_keep_on_overflow = max(1, int(summary_keep_on_overflow))
        if self.summary_keep_on_overflow > self.summary_max_lines:
            self.summary_keep_on_overflow = self.summary_max_lines
        self.step_logger = step_logger
        self.verbose = verbose
        self.summary_lines: List[str] = []
        self.recent_steps: List[Dict[str, Any]] = []

    def _log_step(
        self,
        iteration: int,
        thought: str,
        action: str,
        action_input: Any,
        observation: Optional[str] = None,
        final: Optional[str] = None,
    ) -> None:
        if not self.step_logger:
            return
        record: Dict[str, Any] = {
            "iteration": iteration,
            "thought": thought,
            "action": action,
            "action_input": _sanitize_log_value(action_input),
        }
        if observation is not None:
            record["observation"] = _sanitize_log_value(observation)
        if final is not None:
            record["final"] = final
        self.step_logger.log(record)

    def _shorten(self, text: str, max_tokens: int) -> str:
        return truncate_text_by_tokens(text, max_tokens).strip()

    def _rollup_old_steps(self, keep_last: Optional[int] = None) -> None:
        keep_last_steps = self.max_recent_steps_count if keep_last is None else max(1, int(keep_last))
        if len(self.recent_steps) <= keep_last_steps:
            return

        old = self.recent_steps[:-keep_last_steps]
        self.recent_steps = self.recent_steps[-keep_last_steps:]
        if self.verbose:
            cprint(
                f"[context] rollup: summarizing {len(old)} old step(s), keeping last {keep_last_steps}",
                color="yellow",
            )
        for step in old:
            action = step.get("action", "")
            action_input = step.get("action_input", "")
            obs = step.get("observation", "")
            action_input_str = (
                action_input
                if isinstance(action_input, str)
                else json.dumps(action_input, ensure_ascii=False)
            )
            line = (
                f"{action}({self._shorten(action_input_str, 64)})"
                f" -> {self._shorten(str(obs), 96)}"
            )
            self.summary_lines.append(line)

        if len(self.summary_lines) > self.summary_max_lines:
            self.summary_lines = self.summary_lines[-self.summary_max_lines:]

        if self.verbose:
            cprint(
                f"[context] summary_lines={len(self.summary_lines)}, recent_steps={len(self.recent_steps)}",
                color="yellow",
            )

    def _render_context(self, task: str) -> str:
        c_files = [f for f in os.listdir(self.base_dir) if f.lower().endswith((".c", ".h"))]
        c_files.sort()

        summary = (
            "\n".join(f"- {line}" for line in self.summary_lines[-self.summary_render_lines:])
            if self.summary_lines
            else "(none)"
        )

        steps: List[str] = []
        for idx, step in enumerate(self.recent_steps[-self.max_recent_steps_count:], start=1):
            action_input = step.get("action_input", "")
            action_input_str = (
                action_input
                if isinstance(action_input, str)
                else json.dumps(action_input, ensure_ascii=False)
            )

            action = step.get("action", "")
            obs = str(step.get("observation", ""))

            if action in ("Read Code Slice", "Terminal"):
                obs_display = obs
            else:
                obs_display = self._shorten(obs, MAX_PER_HISTORY_MESSAGE_TOKENS)

            steps.append(
                "\n".join(
                    [
                        f"[Step {idx}]",
                        f"Action: {action}",
                        f"Action Input: {self._shorten(action_input_str, 256)}",
                        f"Observation: {obs_display}",
                    ]
                )
            )
        recent = "\n\n".join(steps) if steps else "(none)"

        return f"""{task.strip()}

Target directory: {self.base_dir}
C files in directory: {", ".join(c_files) if c_files else "(none found)"}

Available tools:
{_format_tool_catalog(self.tools)}

Memory summary (rolled up):
{summary}

Recent tool steps:
{recent}

Now decide the next step.
Return STRICT JSON only, in one of these forms:

1) Tool call:
{{
  "thought": "...",
  "action": "<one of the tool names above>",
  "action_input": "<string or object>"
}}

2) Finish:
{{
  "thought": "...",
  "action": "FINAL",
  "final": "Explain what you changed and the last compilation result."
}}
"""

    def _build_messages(
        self, task: str, last_error: Optional[str] = None
    ) -> List[Dict[str, str]]:
        user = self._render_context(task)
        if last_error:
            user = f"{user}\n\nPrevious response had an error: {last_error}\nReturn valid JSON only."
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}]

        while True:
            token_count = sum(len(enc.encode(m["content"])) for m in messages)
            if token_count <= self.max_context_tokens:
                return messages

            if self.verbose:
                cprint(
                    f"[context] over limit: {_fmt_token_usage(token_count, self.max_context_tokens)}; applying compression",
                    color="yellow",
                )
            self._rollup_old_steps(keep_last=self.max_recent_steps_overflow_count)
            if self.summary_lines:
                self.summary_lines = self.summary_lines[-self.summary_keep_on_overflow:]
            if self.recent_steps:
                for step in self.recent_steps:
                    step["observation"] = self._shorten(str(step.get("observation", "")), 256)

            if self.verbose:
                tmp_user = self._render_context(task)
                tmp_msgs = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": tmp_user}]
                tmp_token_count = sum(len(enc.encode(m["content"])) for m in tmp_msgs)
                cprint(
                    f"[context] after compression: {_fmt_token_usage(tmp_token_count, self.max_context_tokens)}",
                    color="yellow",
                )

            user = self._render_context(task)
            if last_error:
                user = f"{user}\n\nPrevious response had an error: {last_error}\nReturn valid JSON only."
            messages[1] = {"role": "user", "content": user}

    def run(self, task: str, success_signal: Optional[str] = None) -> Dict[str, Any]:
        """Run the ReAct loop.

        Args:
            task: The task description / user prompt.
            success_signal: Optional string; if found in an observation the
                            loop terminates early with success. Defaults to
                            "Compilation succeeded".

        Returns:
            {"output": str, "status": "success"|"failure"}
        """
        if success_signal is None:
            success_signal = "Compilation succeeded"

        last_parse_error: Optional[str] = None

        for iteration in range(1, self.max_iterations + 1):
            self._rollup_old_steps()
            messages = self._build_messages(task, last_error=last_parse_error)
            last_parse_error = None

            if self.verbose:
                token_count = sum(token_len(m["content"]) for m in messages)
                cprint(f"\n[iteration {iteration}] context={_fmt_token_usage(token_count, self.max_context_tokens)}")

            response = call_llm(self.client, messages, max_tokens=LLM_MAX_OUTPUT_TOKENS)
            thought, action, action_input, final_answer = _parse_react_response(response)

            if self.verbose:
                cprint(f"[llm response] tokens={token_len(response)}")

            if not action:
                last_parse_error = "Missing `action`."
                if self.verbose:
                    cprint(f"[parse] error: {last_parse_error}", color="red")
                continue

            if action.upper() == "FINAL":
                final = (final_answer or response).strip()
                if self.verbose:
                    cprint("[final] agent requested FINAL")
                self._log_step(
                    iteration=iteration,
                    thought=thought,
                    action="FINAL",
                    action_input=action_input,
                    final=final,
                )
                return {
                    "output": final,
                    "status": _status_from_output(final),
                }

            tool = self.tools.get(action)
            if tool is None:
                last_parse_error = f"Unknown tool: {action}. Valid tools: {', '.join(self.tools)}"
                if self.verbose:
                    cprint(f"[parse] error: {last_parse_error}", color="red")
                continue

            tool_input_str: str
            if isinstance(action_input, (dict, list)):
                tool_input_str = json.dumps(action_input, ensure_ascii=False)
            else:
                tool_input_str = str(action_input)

            if self.verbose:
                cprint(f"Thought: {thought}", color="green", bold=True)
                cprint(f"Action: {action}", color="green", bold=True)
                cprint(f"Action Input: (tokens={token_len(tool_input_str)})", color="green", bold=True)
                cprint(_safe_preview(tool_input_str, PRINT_MAX_MESSAGE_TOKENS) or "(empty)", color="green", bold=True)

            try:
                observation = tool.func(tool_input_str)
            except Exception as exc:
                observation = f"Tool execution failed: {exc}\n{traceback.format_exc()}"

            if self.verbose:
                cprint(f"Observation: (tokens={token_len(observation)})", color="yellow", bold=True)
                cprint(_safe_preview(observation, PRINT_MAX_OBSERVATION_TOKENS) or "(empty)", color="yellow", bold=True)

            self._log_step(
                iteration=iteration,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation,
            )
            self.recent_steps.append(
                {
                    "thought": thought,
                    "action": action,
                    "action_input": action_input,
                    "observation": observation,
                }
            )

            if success_signal in observation:
                return {
                    "output": success_signal,
                    "status": "success",
                }

        return {
            "output": f"Reached max iterations ({self.max_iterations}) without finishing.",
            "status": "failure",
        }


# ---------------------------------------------------------------------------
# Compilation helpers
# ---------------------------------------------------------------------------


def compile_c_file(base_dir: str, c_file: str, compile_cmd: str = DEFAULT_COMPILE_CMD) -> Tuple[bool, str]:
    """Compile a .c file and return (success: bool, output: str)."""
    try:
        proc = subprocess.run(
            f"{compile_cmd} {c_file}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            text=True,
            shell=True,
            errors="ignore",
            cwd=base_dir,
        )
        output = f"{proc.stderr}\n{proc.stdout}".strip()
        success = proc.returncode == 0
        return success, output
    except Exception as exc:
        return False, f"Compilation failed: {exc}"


def verify_compilation(base_dir: str, c_file: str, compile_cmd: str = DEFAULT_COMPILE_CMD) -> Tuple[bool, str]:
    """Verify that a .c file compiles cleanly. Returns (pass: bool, message: str)."""
    success, output = compile_c_file(base_dir, c_file, compile_cmd)
    if success:
        return True, "Compilation succeeded."
    return False, output


# ---------------------------------------------------------------------------
# File integrity helpers
# ---------------------------------------------------------------------------


def get_file_metrics(filepath: str) -> Tuple[int, int]:
    """Return (line_count, byte_count) for a file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return lines, len(content.encode("utf-8"))
    except Exception:
        return 0, 0


def check_file_integrity(filepath: str, baseline_lines: int) -> Tuple[bool, str]:
    """Check that a file is non-empty and hasn't lost >20% of its lines."""
    if not os.path.exists(filepath):
        return False, "FATAL: file does not exist!"
    if os.path.getsize(filepath) == 0:
        return False, "FATAL: file is empty!"
    current_lines, _ = get_file_metrics(filepath)
    if baseline_lines > 0 and current_lines < baseline_lines * 0.8:
        return False, f"FATAL: file truncated! {baseline_lines} → {current_lines} lines (>{20}% loss)"
    return True, f"OK: {current_lines} lines"
