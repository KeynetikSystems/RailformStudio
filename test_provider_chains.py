# -*- coding: utf-8 -*-
"""Headless test of provider chain validation.

Run with: freecadcmd test_provider_chains.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import FreeCAD
from rfstudio import engines, deps


def out(s):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def test_provider_chains_resolvable():
    """Test that all provider chains are valid and resolvable."""
    problems = engines.validate_providers()
    if problems:
        out("PROVIDER CHAIN PROBLEMS:")
        for p in problems:
            out(f"  {p}")
        return False
    out("Provider chains OK")
    return True


def test_critical_commands_have_native_fallback():
    """Critical commands must have a native fallback (not engine-only)."""
    single_points = deps.report_single_points_of_failure(engines.PROVIDERS)
    critical = ["RFS_Trim", "RFS_SketchOnSurf", "RFS_BlendSurf"]
    failed = []
    for cmd in critical:
        if cmd in single_points:
            out(f"CRITICAL: {cmd} has no native fallback!")
            failed.append(cmd)
    if failed:
        return False
    out("Critical commands have native fallbacks")
    return True


def test_delegations_match_providers():
    """Every action in DELEGATIONS must have a provider chain."""
    for cmd_id in engines.DELEGATIONS:
        if cmd_id not in engines.PROVIDERS:
            out(f"MISSING PROVIDER CHAIN: {cmd_id}")
            return False
    out("All delegations have provider chains")
    return True


def test_no_empty_provider_chains():
    """No command should have an empty provider chain."""
    for cmd_id, chain in engines.PROVIDERS.items():
        if not chain:
            out(f"EMPTY PROVIDER CHAIN: {cmd_id}")
            return False
    out("No empty provider chains")
    return True


# Run tests at module level (like test_core.py does)
all_passed = True

out("\nRunning test_provider_chains_resolvable...")
if not test_provider_chains_resolvable():
    all_passed = False

out("\nRunning test_critical_commands_have_native_fallback...")
if not test_critical_commands_have_native_fallback():
    all_passed = False

out("\nRunning test_delegations_match_providers...")
if not test_delegations_match_providers():
    all_passed = False

out("\nRunning test_no_empty_provider_chains...")
if not test_no_empty_provider_chains():
    all_passed = False

out("\n" + "="*50)
if all_passed:
    out("ALL TESTS PASSED")
    sys.exit(0)
else:
    out("SOME TESTS FAILED")
    sys.exit(1)