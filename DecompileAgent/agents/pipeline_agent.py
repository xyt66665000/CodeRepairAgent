"""
Pipeline Orchestrator Agent

Orchestrates the full 5-phase decompiled C code repair pipeline,
matching the `repair-full-pipeline` skill.

Pipeline order:
  Phase 1: decompile-repair        (always runs)
  Phase 2: restore-decompiled-structs (gated: PAMA/degradation)
  Phase 3: restore-function-signatures (gated: generic signatures)
  Phase 4: variable-semantic-recovery   (gated: v1-vN / scalar-in-ptr)
  Phase 5: control-flow-normalizer      (gated: goto/label patterns)

Each phase: gate check → backup → run → integrity check → compile verify.
Failed phases rollback after 3 attempts and the pipeline continues.
"""

import os
import sys
import shutil
import datetime
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from agents.base_agent import (
    DEFAULT_COMPILE_CMD,
    verify_compilation,
    get_file_metrics,
    check_file_integrity,
    cprint,
)
from agents.decompile_repair_agent import (
    gate_check as phase1_gate,
    run_decompile_repair,
)
from agents.struct_restore_agent import (
    gate_check as phase2_gate,
    run_struct_restore,
)
from agents.function_signature_agent import (
    gate_check as phase3_gate,
    run_function_signature_restore,
)
from agents.variable_semantic_agent import (
    gate_check as phase4_gate,
    run_variable_semantic_recovery,
)
from agents.control_flow_agent import (
    gate_check as phase5_gate,
    run_control_flow_normalize,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Phase configuration
# ---------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS = 2


class PhaseConfig:
    """Configuration for a single pipeline phase."""

    def __init__(
        self,
        phase_num: int,
        name: str,
        gate_fn,
        run_fn,
        run_fn_kwargs: Optional[Dict] = None,
    ):
        self.phase_num = phase_num
        self.name = name
        self.gate_fn = gate_fn
        self.run_fn = run_fn
        self.run_fn_kwargs = run_fn_kwargs or {}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _backup_file(filepath: str, phase_num: int) -> str:
    """Create a backup of the file before a phase runs."""
    backup_path = f"{filepath}.bak.phase{phase_num}"
    shutil.copy2(filepath, backup_path)
    return backup_path


def _restore_from_backup(filepath: str, backup_path: str) -> None:
    """Restore file from backup."""
    shutil.copy2(backup_path, filepath)


def _delete_backup(backup_path: str) -> None:
    """Delete a backup file."""
    if os.path.exists(backup_path):
        os.remove(backup_path)


# ---------------------------------------------------------------------------
# Pipeline result tracking
# ---------------------------------------------------------------------------


class PhaseResult:
    """Tracks the result of a single pipeline phase."""

    def __init__(self, phase_num: int, name: str):
        self.phase_num = phase_num
        self.name = name
        self.skipped: bool = False
        self.skip_reason: str = ""
        self.rolled_back: bool = False
        self.rollback_reason: str = ""
        self.success: bool = False
        self.message: str = ""
        self.details: Dict[str, Any] = {}

    @property
    def status(self) -> str:
        if self.skipped:
            return f"SKIPPED: {self.skip_reason}"
        if self.rolled_back:
            return f"ROLLED BACK after {MAX_RETRY_ATTEMPTS} failed attempts. Reason: {self.rollback_reason}"
        if self.success:
            return "OK"
        return f"FAILED: {self.message}"


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_pipeline(
    base_dir: str,
    c_file: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    phases_to_run: Optional[List[int]] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run the full 5-phase repair pipeline on a single .c file.

    Args:
        base_dir: Directory containing the .c file.
        c_file: Name of the .c file to process.
        compile_cmd: GCC compile command to use for verification.
        phases_to_run: Optional list of phase numbers to run (default: phases 1-2).
        verbose: Print detailed progress.

    Returns:
        Dict with pipeline results and final report.
    """
    filepath = os.path.join(base_dir, c_file)
    if not os.path.exists(filepath):
        return {
            "status": "failure",
            "message": f"File not found: {filepath}",
        }

    if phases_to_run is None:
        phases_to_run = [1, 2]

    # Phase configurations
    phases: List[PhaseConfig] = [
        PhaseConfig(1, "decompile-repair", phase1_gate, run_decompile_repair,
                    {"base_dir": base_dir, "compile_cmd": compile_cmd, "verbose": verbose}),
        PhaseConfig(2, "restore-decompiled-structs", phase2_gate, run_struct_restore,
                    {"base_dir": base_dir, "c_file": c_file, "compile_cmd": compile_cmd, "verbose": verbose}),
        PhaseConfig(3, "restore-function-signatures", phase3_gate, run_function_signature_restore,
                    {"base_dir": base_dir, "c_file": c_file, "compile_cmd": compile_cmd, "verbose": verbose}),
        PhaseConfig(4, "variable-semantic-recovery", phase4_gate, run_variable_semantic_recovery,
                    {"base_dir": base_dir, "c_file": c_file, "compile_cmd": compile_cmd, "verbose": verbose}),
        PhaseConfig(5, "control-flow-normalizer", phase5_gate, run_control_flow_normalize,
                    {"base_dir": base_dir, "c_file": c_file, "compile_cmd": compile_cmd, "verbose": verbose}),
    ]

    results: List[PhaseResult] = []
    rollback_count = 0

    cprint(f"\n{'='*60}", color="blue", bold=True)
    cprint(f"Repair Full Pipeline — {c_file}", color="blue", bold=True)
    cprint(f"{'='*60}\n", color="blue", bold=True)

    for phase in phases:
        if phase.phase_num not in phases_to_run:
            continue

        result = PhaseResult(phase.phase_num, phase.name)
        cprint(f"[Phase {phase.phase_num}] {phase.name}", color="blue", bold=True)

        # ── Step 1: Gate check ──
        if phase.phase_num == 1:
            should_run, reason = True, "Phase 1 always runs (entry point)"
        else:
            should_run, reason = phase.gate_fn(filepath)

        if not should_run:
            result.skipped = True
            result.skip_reason = reason
            result.success = True
            cprint(f"  -> SKIPPED: {reason}", color="yellow")
            results.append(result)
            continue

        cprint(f"  -> RUNNING: {reason}")

        # ── Step 2: Backup ──
        baseline_lines, _ = get_file_metrics(filepath)
        backup_path = _backup_file(filepath, phase.phase_num)
        cprint(f"  -> Backup: {backup_path}")

        # ── Step 3: Run phase (with retry) ──
        phase_success = False
        last_error = ""

        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            cprint(f"  -> Attempt {attempt}/{MAX_RETRY_ATTEMPTS}")

            success, message = phase.run_fn(**phase.run_fn_kwargs)

            # ── Step 4: Integrity check ──
            ok, integrity_msg = check_file_integrity(filepath, baseline_lines)
            if not ok:
                cprint(f"  -> FILE INTEGRITY FAILURE: {integrity_msg}", color="red")
                _restore_from_backup(filepath, backup_path)
                last_error = f"File integrity failure: {integrity_msg}"
                continue

            # ── Step 5: Compilation verification ──
            compile_ok, compile_msg = verify_compilation(base_dir, c_file, compile_cmd)
            if compile_ok:
                phase_success = True
                result.success = True
                result.message = message
                _delete_backup(backup_path)
                cprint(f"  -> OK: compilation verified", color="green")
                break
            else:
                cprint(f"  -> Compilation FAILED (attempt {attempt}): {compile_msg[:200]}", color="red")
                last_error = compile_msg

        # ── Step 6: Handle failure / rollback ──
        if not phase_success:
            result.rolled_back = True
            result.rollback_reason = last_error[:500]
            _restore_from_backup(filepath, backup_path)
            _delete_backup(backup_path)
            rollback_count += 1
            cprint(
                f"  -> ROLLED BACK after {MAX_RETRY_ATTEMPTS} failed attempts. "
                f"Reason: {last_error[:200]}",
                color="red",
            )
        else:
            # Verify compilation one more time for reliability
            compile_ok, _ = verify_compilation(base_dir, c_file, compile_cmd)
            if not compile_ok:
                cprint(f"  -> WARNING: Post-phase compilation verification failed", color="yellow")

        results.append(result)

    # ── Final report ──
    report = _generate_report(c_file, results, base_dir, compile_cmd)
    cprint(report, color="blue")

    # Save pipeline result
    from utils.logger import save_agent_result
    save_agent_result(
        base_dir,
        result={
            "c_file": c_file,
            "phases": [
                {
                    "num": r.phase_num,
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                }
                for r in results
            ],
            "report": report,
        },
        agent_name="pipeline",
    )

    return {
        "status": "success",
        "message": f"Pipeline complete. {len([r for r in results if r.success])}/{len(results)} phases passed, {rollback_count} rollback(s).",
        "results": results,
        "report": report,
    }


def _generate_report(
    c_file: str,
    results: List[PhaseResult],
    base_dir: str,
    compile_cmd: str,
) -> str:
    """Generate the final pipeline report."""
    lines = [
        "=" * 60,
        "=== Repair Full Pipeline Complete ===",
        "=" * 60,
        "",
    ]

    for r in results:
        lines.append(f"Phase {r.phase_num} ({r.name}):")
        if r.skipped:
            lines.append(f"  - {r.status}")
        elif r.rolled_back:
            lines.append(f"  - {r.status}")
        else:
            lines.append(f"  - {r.status}")
            if r.message:
                # Truncate long messages
                msg = r.message[:300]
                lines.append(f"  - {msg}")
        lines.append("")

    # Compilation verification
    compile_ok, _ = verify_compilation(base_dir, c_file, compile_cmd)
    lines.append(f"Rollbacks: {sum(1 for r in results if r.rolled_back)} phase(s) rolled back")
    lines.append(f"Compilation verified: {'yes' if compile_ok else 'no'}")
    lines.append(f"Final artifacts: {c_file}" + (f", {c_file[:-2]}.o" if compile_ok else ""))
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def run_pipeline_batch(
    base_dir: str,
    compile_cmd: str = DEFAULT_COMPILE_CMD,
    phases_to_run: Optional[List[int]] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run the pipeline on all .c files in a directory.

    If multiple .c files exist, each is processed in its own subdirectory.
    """
    if not os.path.isdir(base_dir):
        return {"status": "failure", "message": f"Not a directory: {base_dir}"}

    c_files = [f for f in os.listdir(base_dir) if f.endswith(".c")]
    if not c_files:
        return {"status": "failure", "message": f"No .c files found in {base_dir}"}

    cprint(f"Found {len(c_files)} .c file(s): {', '.join(c_files)}", color="blue")

    # If multiple .c files, move each to its own subdirectory
    if len(c_files) > 1:
        for fname in c_files:
            name = os.path.splitext(fname)[0]
            subdir = os.path.join(base_dir, name)
            os.makedirs(subdir, exist_ok=True)
            src = os.path.join(base_dir, fname)
            dst = os.path.join(subdir, fname)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.move(src, dst)
                cprint(f"Moved: {fname} -> {subdir}/", color="blue")

    # Process each subdirectory
    all_results = []
    for item in sorted(os.listdir(base_dir)):
        folder_path = os.path.join(base_dir, item)
        if not os.path.isdir(folder_path):
            continue

        sub_c_files = [f for f in os.listdir(folder_path) if f.endswith(".c")]
        if not sub_c_files:
            continue

        cprint(f"\nProcessing folder: {folder_path}", color="blue", bold=True)
        for c_file in sub_c_files:
            result = run_pipeline(
                base_dir=folder_path,
                c_file=c_file,
                compile_cmd=compile_cmd,
                phases_to_run=phases_to_run,
                verbose=verbose,
            )
            all_results.append(result)

    return {
        "status": "success",
        "message": f"Batch pipeline complete. Processed {len(all_results)} file(s).",
        "results": all_results,
    }
