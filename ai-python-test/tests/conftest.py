import sys
from pathlib import Path

# El servicio se ejecuta desde app/ (así lo hace el Dockerfile), replicamos ese path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
