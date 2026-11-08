"""Write docs/openapi.json from the FastAPI app (no database or broker needed).

python scripts/export_openapi.py
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("JWT_SECRET", "x" * 32)

from priorauth.api.main import create_app  # noqa: E402

spec = create_app(publisher=object(), instrument=False).openapi()
out = ROOT / "docs" / "openapi.json"
out.write_text(json.dumps(spec, indent=2) + "\n")
print(f"wrote {out.relative_to(ROOT)} ({len(spec['paths'])} paths)")
