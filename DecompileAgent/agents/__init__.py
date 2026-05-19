"""
CodeFixAgent — Multi-Agent Decompiled C Code Repair Pipeline

A 5-phase orchestrated system for repairing and semantically restoring
decompiled C pseudocode.

Phases:
  1. decompile_repair           — Make code compilable with strict gcc
  2. struct_restore              — Restore degraded structs, eliminate PAMA
  3. function_signature          — Restore return types, params, conventions
  4. variable_semantic           — Recover meaningful variable names/types
  5. control_flow                — Normalize goto/label into structured CFG

Orchestrator:
  pipeline_agent                 — Runs all phases with gates, retry, rollback

Shared infrastructure:
  base_agent                     — ReAct agent loop, tools, helpers
  summary_agent                  — Function summarization utility
"""

__all__ = [
    # Pipeline orchestrator
    "pipeline_agent",
    # Phase agents
    "decompile_repair_agent",
    "struct_restore_agent",
    "function_signature_agent",
    "variable_semantic_agent",
    "control_flow_agent",
    # Infrastructure
    "base_agent",
    "summary_agent",
]
