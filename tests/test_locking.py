import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.locking import ProcessLock


def test_lock_contends_but_stale_file_does_not_block(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"

    with ProcessLock(path), pytest.raises(RuntimeError, match="LOCK_CONTENDED"), ProcessLock(path):
        pass

    with ProcessLock(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_lock_is_released_after_holder_exits_without_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    root = Path(__file__).parents[1]
    environment = os.environ | {"PYTHONPATH": str(root / "src")}
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from common.locking import ProcessLock\n"
        "ProcessLock(Path(sys.argv[1])).__enter__()\n"
        "os._exit(0)\n"
    )

    subprocess.run([sys.executable, "-c", code, str(path)], env=environment, check=True)

    with ProcessLock(path):
        pass
