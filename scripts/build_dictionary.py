"""Build the offline dictionary used by /w from ECDICT's ecdict.csv.

ECDICT's Chinese meanings are Simplified Chinese; they're converted to
Traditional Chinese (Taiwan phrasing, OpenCC's s2twp) here, once, so the
gateway can read the result with nothing but the standard library.

Needs the OpenCC Python package on the machine that runs this script (not
in the gateway container):

    pip install opencc
    curl -LO https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
    python scripts/build_dictionary.py ecdict.csv <profile workspace>/dictionary/ecdict.sqlite

See docs/DICTIONARY.md.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from openclaw_runtime.dictionary import build_dictionary_db  # noqa: E402


def make_converter():
    try:
        import opencc
    except ImportError:
        sys.exit("OpenCC is not installed -- run: pip install opencc")
    for config in ("s2twp", "s2twp.json"):
        try:
            return opencc.OpenCC(config).convert
        except Exception:  # noqa: BLE001 - config naming differs between opencc releases
            continue
    sys.exit("Could not load OpenCC's s2twp configuration")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path, help="path to ECDICT's ecdict.csv")
    parser.add_argument("db_path", type=Path, help="SQLite file to write, e.g. .../dictionary/ecdict.sqlite")
    args = parser.parse_args()

    started = time.monotonic()
    count = build_dictionary_db(args.csv_path, args.db_path, make_converter())
    size_mb = args.db_path.stat().st_size / 1_000_000
    print(f"Wrote {count} entries to {args.db_path} ({size_mb:.0f} MB) in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
