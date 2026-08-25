from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observer.cli import main


raise SystemExit(main(["ingest", "--stdin", "--root", str(ROOT), *sys.argv[1:]]))
