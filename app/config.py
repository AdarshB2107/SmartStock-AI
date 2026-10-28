from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "yottaflex.db"
SEED = 20261022
DEFAULT_SCALE = {"n_skus": 50, "n_stores": 5, "n_days": 90}
FULL_SCALE = {"n_skus": 2000, "n_stores": 100, "n_days": 365}
