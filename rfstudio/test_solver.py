#!/usr/bin/env python3
"""Test script for the _solve_linear_equality_constrained_qp function."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'rfstudio'))

try:
    import numpy as np
    import scipy.sparse as sp

    # Import our function
    from core import _solve_linear_equality_constrained_qp

    # Test 1: Simple 2D problem
    # minimize (1/2) * (x1^2 + x2^2)
    # subject to x1 + x2 = 1
    # Solution should be x1 = x2 = 0.5

    P = sp.eye(2)  # Identity matrix
    A = sp.csr_matrix([[1.0, 1.0]])  # [1, 1]
    b = np.array([1.0])

    result = _solve_linear_equality_constrained_qp(P, A, b)
    print("Test 1 - Simple QP:")
    print(f"  Result: {result}")
    print(f"  Expected: [0.5, 0.5]")
    if result is not None:
        error = np.linalg.norm(result - np.array([0.5, 0.5]))
        print(f"  Error: {error}")
        print(f"  Pass: {error < 1e-10}")
    else:
        print("  FAIL: Result is None")
    print()

    # Test 2: Infeasible problem
    # minimize (1/2) * x^2
    # subject to 0*x = 1 (no solution)

    P = sp.eye(1)
    A = sp.csr_matrix([[0.0]])
    b = np.array([1.0])

    result = _solve_linear_equality_constrained_qp(P, A, b)
    print("Test 2 - Infeasible QP:")
    print(f"  Result: {result}")
    print(f"  Expected: None")
    print(f"  Pass: {result is None}")
    print()

    # Test 3: Unconstrained problem (should still work)
    # minimize (1/2) * (x1^2 + x2^2)
    # Solution should be x1 = x2 = 0

    P = sp.eye(2)
    A = sp.csr_matrix((0, 2))  # Empty constraint matrix
    b = np.array([])

    result = _solve_linear_equality_constrained_qp(P, A, b)
    print("Test 3 - Unconstrained QP:")
    print(f"  Result: {result}")
    print(f"  Expected: [0.0, 0.0]")
    if result is not None:
        error = np.linalg.norm(result - np.array([0.0, 0.0]))
        print(f"  Error: {error}")
        print(f"  Pass: {error < 1e-10}")
    else:
        print("  FAIL: Result is None")
    print()

except Exception as e:
    print(f"Error during test: {e}")
    import traceback
    traceback.print_exc()