#!/usr/bin/env python3
"""
Test runner for NaguMIX project.

Usage:
    python run_tests.py              # Run all tests
    python run_tests.py -v           # Verbose output
    python run_tests.py TestCore     # Run specific test class
"""

import unittest
import sys
import os

# Add project root to Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)


def run_tests(verbosity=1, pattern='test*.py', test_name=None):
    """Run the test suite."""
    # Discover and run tests
    test_dir = os.path.join(project_root, 'tests')

    if test_name:
        # Run specific test
        suite = unittest.TestLoader().loadTestsFromName(f'tests.{test_name}')
    else:
        # Discover all tests
        suite = unittest.TestLoader().discover(test_dir, pattern=pattern)

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Return exit code based on test results
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Run NaguMIX tests')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose test output')
    parser.add_argument('test_name', nargs='?',
                        help='Specific test class or method to run')

    args = parser.parse_args()

    verbosity = 2 if args.verbose else 1
    exit_code = run_tests(verbosity=verbosity, test_name=args.test_name)

    sys.exit(exit_code)
