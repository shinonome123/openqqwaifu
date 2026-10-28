# Single modules (python -m unittest tests.test_app) run in a temporary working directory too.
from . import test__isolated_working_directory  # noqa: F401
