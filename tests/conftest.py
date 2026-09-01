"""Shared pytest fixtures (opt-in — tests currently run as standalone scripts).

The mock staging server is started once per session, on 127.0.0.1:18801,
and shared across all tests. It is bundled with the project
(specter/mock_server.py) and exposes the same endpoints the agent tests
against.

Tests in this repo currently run as standalone scripts (e.g.
`python3 tests/test_e2e_integration.py`) using a custom TestContext
runner, not pytest. This conftest is provided for the future pytest
migration: install pytest and run `pytest tests/`.

The cf_selenium / cf_persistent modules live in the project root (siblings
of the specter/ package) and are FROZEN — DO NOT MODIFY. This conftest
adds the project root to sys.path so the import graph stays intact.
"""

# placeholder: import real fixtures once pytest is added to dev deps
# import os, sys, time, socket, subprocess
# from pathlib import Path
# import pytest
#
# ROOT = Path(__file__).resolve().parent.parent
# sys.path.insert(0, str(ROOT))
#
# @pytest.fixture(scope="session")
# def mock_server():
#     ...
