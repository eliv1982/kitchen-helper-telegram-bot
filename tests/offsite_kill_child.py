"""The child process of the hard-kill tests (tests/test_deploy_offsite_recovery.py): it runs the
off-host backup over the rig in ROOT, blocks at STAGE (after creating the file READY), and is killed there
by its parent with a kill that no signal handler can see (SIGKILL; TerminateProcess on Windows).

    python -m tests.offsite_kill_child ROOT STAGE READY

Not a test and not part of the tool. Everything it touches is under ROOT, which the parent made.
"""

import sys
from pathlib import Path

from tests.offsite_support import Rig

root, stage, ready = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
rig = Rig(root, fresh=False)
rig.fake.hang_at = (stage, ready)
sys.exit(rig.run())
