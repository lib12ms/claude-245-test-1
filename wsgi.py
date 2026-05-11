import importlib.util
from pathlib import Path

base_dir = Path(__file__).resolve().parent
app_file = base_dir / "app.py"

spec = importlib.util.spec_from_file_location("app_module", app_file)
if spec is None or spec.loader is None:
    raise RuntimeError("Failed to load app.py")

module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

app = module.app

