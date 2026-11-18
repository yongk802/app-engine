"""The test client addresses the engine as ``testserver``; only the suite may call that a local name."""

import os

os.environ["APP_ENGINE_TESTING"] = "1"
