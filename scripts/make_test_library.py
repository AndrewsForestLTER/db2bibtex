#!/usr/bin/env python3
"""
make_test_library.py

Creates a modified copy of a Zotero library export with a known, specific
set of entries removed -- for testing db2bibtex-compare against a positive
control (we know exactly which publication_ids should be reported missing).

Usage:
    python make_test_library.py --library library_export.bib \
        --output library_export_test.bib --count 5

Reuses db2bibtex.compare's own parsing functions, so this test exercises
the exact same entry-splitting logic the tool itself uses -- not a
reimplementation that could drift from it.
"""

import argparse
import random

from db2bibtex.compare import split_entries, extract_pub_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True)
    parser.add_argument("--output", default="library_export_test.bib")
    parser.add_argument("--count", type=int, default=5,
                         help="Number of entries to remove")
    parser.add_argument("--seed", type=int, default=42,
                         help="Random seed, for a reproducible selection")
    args = parser.parse_args()

    with open(args.library, "r", encoding="utf-8") as f:
        text = f.read()

    entries = split_entries(text)
    print(f"Library has {len(entries)} entries")

    if args.count > len(entries):
        raise SystemExit(f"--count {args.count} exceeds library size {len(entries)}")

    random.seed(args.seed)
    remove_idx = set(random.sample(range(len(entries)), args.count))

    removed = []
    kept = []
    for i, entry in enumerate(entries):
        pid = extract_pub_id(entry)
        if i in remove_idx:
            removed.append((pid, entry.split(",", 1)[0].strip()))
        else:
            kept.append(entry)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(f"% Test library export -- {len(kept)} of {len(entries)} entries "
                 f"(deliberately missing {len(removed)} for compare testing)\n\n")
        f.write("\n\n".join(kept))
        f.write("\n")

    print(f"Wrote {len(kept)} entries to {args.output}")
    print(f"\nDeliberately removed {len(removed)} entries -- expect exactly these "
          f"publication_ids to show up as 'missing':")
    for pid, key in sorted(removed, key=lambda x: int(x[0]) if x[0] else 0):
        print(f"  publication_id {pid}  ({key})")


if __name__ == "__main__":
    main()