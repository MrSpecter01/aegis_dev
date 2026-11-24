import os
import sys
import pytest
from pathlib import Path
import pydantic.root_model

# Ensure root directory and .deps are in sys.path for test resolution
ROOT_DIR = Path(__file__).parent.parent
DEPS_DIR = ROOT_DIR / ".deps"
_PYTEST_TMP = ROOT_DIR / ".pytest_tmp"
_LOCAL_APP_DATA = _PYTEST_TMP / "localappdata"
_LOCAL_APP_DATA.mkdir(parents=True, exist_ok=True)

# Set temporary directories to project-local to avoid Windows ACL restrictions
os.environ["TEMP"] = str(_PYTEST_TMP)
os.environ["TMP"] = str(_PYTEST_TMP)
os.environ["TMPDIR"] = str(_PYTEST_TMP)
os.environ["LOCALAPPDATA"] = str(_LOCAL_APP_DATA)
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"

if sys.platform == "win32":
    pywin32_dir = ROOT_DIR / ".venv" / "Lib" / "site-packages" / "pywin32_system32"
    if pywin32_dir.is_dir():
        try:
            os.add_dll_directory(str(pywin32_dir))
        except Exception:
            pass

if str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load .env files if present (deploy/.env or root .env)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / "deploy" / ".env")
    load_dotenv(ROOT_DIR / ".env")
except ImportError:
    pass


@pytest.fixture()
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Override tmp_path to use a project-local base dir.

    The system TEMP directory on this machine lacks the O_TEMPORARY permission
    required by Python's tempfile module. Using a project-local directory as
    the base avoids this Windows-specific ACL restriction.
    """
    _PYTEST_TMP.mkdir(parents=True, exist_ok=True)
    return tmp_path_factory.mktemp("test")
