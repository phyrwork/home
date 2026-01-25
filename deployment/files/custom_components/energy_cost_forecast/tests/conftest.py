import sys
from pathlib import Path

INTEGRATION_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(INTEGRATION_ROOT))
