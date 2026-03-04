#!/usr/bin/env python3
"""
find_corp_code.py — Find a company's DART corp_code from the cached XML.

Run after the bot has started once (so data/corp_codes.xml exists):

    python find_corp_code.py 파라택시스
    python find_corp_code.py parataxis
    python find_corp_code.py 비트맥스

Then copy the corp_code into CORP_CODE_OVERRIDES in src/dart.py.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CACHE = Path("data/corp_codes.xml")

if not CACHE.exists():
    print(f"ERROR: {CACHE} not found.")
    print("Start the bot once so it downloads the file, then run this script.")
    sys.exit(1)

query = " ".join(sys.argv[1:]).strip().lower() if len(sys.argv) > 1 else ""
if not query:
    print("Usage: python find_corp_code.py <search term>")
    sys.exit(1)

needle = re.sub(r"\s+", "", query)
tree   = ET.parse(CACHE)
found  = []

for item in tree.getroot().findall("list"):
    name = (item.findtext("corp_name") or "").strip()
    code = (item.findtext("corp_code") or "").strip()
    if needle in re.sub(r"\s+", "", name.lower()):
        found.append((code, name))

if not found:
    print(f"No matches for '{query}'")
else:
    print(f"\nFound {len(found)} match(es) for '{query}':\n")
    for code, name in found:
        print(f"  corp_code: {code}   name: {name}")
    print(f"\nAdd the correct line to CORP_CODE_OVERRIDES in src/dart.py:")
    print(f'    "parataxis": "{found[0][0]}",')
