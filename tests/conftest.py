import os
import sys
import tempfile
from pathlib import Path

# Before dj-librarian is imported: the tests must never touch the files of your real library
os.environ["DJ_LIBRARIAN_HOME"] = tempfile.mkdtemp(prefix="dj-librarian-test-")
sys.path.insert(0, str(Path(__file__).parent))
