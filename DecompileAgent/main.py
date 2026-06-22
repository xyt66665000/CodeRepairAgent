#!/usr/bin/env python3
"""
CodeFixAgent — Multi-Agent Decompiled C Code Repair Pipeline

A 5-phase orchestrated pipeline for repairing and semantically restoring
IDA Pro / Hex-Rays / Ghidra decompiled C pseudocode.

Pipeline order:
  Phase 1: decompile-repair           — Make code compilable with strict gcc
  Phase 2: restore-decompiled-structs  — Restore degraded structs, eliminate PAMA
  Phase 3: restore-function-signatures — Restore return types, params, conventions
  Phase 4: variable-semantic-recovery  — Recover meaningful variable names/types
  Phase 5: control-flow-normalizer     — Normalize goto/label into structured CFG

Usage:
  python main.py -d /path/to/dir        # Run full pipeline on all .c files
  python main.py -d /path/to/dir -p 1   # Run only Phase 1
  python main.py -d /path/to/dir -p 1,2,3  # Run specific phases
"""

import os
import sys
import argparse

from dotenv import load_dotenv

from agents.base_agent import DEFAULT_COMPILE_CMD
from agents.pipeline_agent import run_pipeline, run_pipeline_batch
from agents.decompile_repair_agent import run_decompile_repair
from agents.struct_restore_agent import run_struct_restore
from agents.function_signature_agent import run_function_signature_restore
from agents.variable_semantic_agent import run_variable_semantic_recovery
from agents.control_flow_agent import run_control_flow_normalize
from utils.color_print import cprint

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="CodeFixAgent — Multi-Agent Decompiled C Code Repair Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py -d ./output                # Full pipeline
  python main.py -d ./output -p 1           # Phase 1 only (compile repair)
  python main.py -d ./output -p 1,2         # Phases 1-2 only
  python main.py -d ./output -f test.c      # Process single file
  python main.py -d ./output --single       # Single file mode (dir IS the file's dir)
        """,
    )

    parser.add_argument(
        "-d", "--directory",
        type=str,
        default=os.getcwd(),
        help="Target directory (default: current directory)",
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="Specific .c file to process (if omitted, processes all .c files)",
    )
    parser.add_argument(
        "-p", "--phases",
        type=str,
        default=None,
        help="Comma-separated phase numbers to run, e.g. '1,2,3' (default: all 1-5)",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help="Single-file mode: the directory itself contains the .c file (no subdirectory creation)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Print detailed progress (default: on)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress detailed progress output",
    )

    args = parser.parse_args()
    base_dir = os.path.abspath(args.directory)
    verbose = not args.quiet

    if not os.path.isdir(base_dir):
        cprint(f"Error: Not a directory: {base_dir}", color="red")
        sys.exit(1)

    compile_cmd = DEFAULT_COMPILE_CMD

    # Parse phases
    phases_to_run = None
    if args.phases:
        try:
            phases_to_run = [int(p.strip()) for p in args.phases.split(",")]
            for p in phases_to_run:
                if p < 1 or p > 5:
                    cprint(f"Error: Invalid phase number: {p}. Must be 1-5.", color="red")
                    sys.exit(1)
        except ValueError:
            cprint(f"Error: Invalid phases format: {args.phases}. Use comma-separated numbers.", color="red")
            sys.exit(1)

    cprint(f"Compile command: {compile_cmd}", color="blue")
    cprint(f"Phases: {phases_to_run or 'all (1-5)'}", color="blue")

    # ── Single file mode ──
    if args.single or args.file:
        c_files = [f for f in os.listdir(base_dir) if f.endswith(".c")]
        if args.file:
            target_file = args.file
        elif len(c_files) == 1:
            target_file = c_files[0]
        else:
            cprint(f"Error: Multiple .c files found. Use -f to specify one, or omit --single for batch mode.", color="red")
            cprint(f"Files: {', '.join(c_files)}", color="red")
            sys.exit(1)

        if not os.path.exists(os.path.join(base_dir, target_file)):
            cprint(f"Error: File not found: {os.path.join(base_dir, target_file)}", color="red")
            sys.exit(1)

        result = run_pipeline(
            base_dir=base_dir,
            c_file=target_file,
            compile_cmd=compile_cmd,
            phases_to_run=phases_to_run,
            verbose=verbose,
        )

        if result.get("status") == "success":
            cprint(f"\nPipeline finished successfully.", color="green", bold=True)
        else:
            cprint(f"\nPipeline failed: {result.get('message')}", color="red", bold=True)
            sys.exit(1)
        return

    # ── Batch mode ──
    result = run_pipeline_batch(
        base_dir=base_dir,
        compile_cmd=compile_cmd,
        verbose=verbose,
    )

    if result.get("status") == "success":
        cprint(f"\nBatch pipeline finished successfully.", color="green", bold=True)
    else:
        cprint(f"\nBatch pipeline failed: {result.get('message')}", color="red", bold=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

