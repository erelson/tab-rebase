#!/usr/bin/env python3
"""Assemble a loadable extension folder per browser from extension/.

    python build.py        ->  dist/firefox/  and  dist/chrome/

Each gets the shared background.js plus that browser's manifest renamed to
manifest.json. Load dist/<browser> in the browser (see README.md). Re-run
after editing anything in extension/.
"""

import json
import os
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "extension")
DIST = os.path.join(ROOT, "dist")
SHARED = ["background.js"]
BROWSERS = ["firefox", "chrome"]


def main():
    for browser in BROWSERS:
        out = os.path.join(DIST, browser)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        for name in SHARED:
            shutil.copy2(os.path.join(SRC, name), out)
        manifest = os.path.join(SRC, f"manifest.{browser}.json")
        with open(manifest, encoding="utf-8") as f:
            json.load(f)  # fail loudly on a JSON typo
        shutil.copy2(manifest, os.path.join(out, "manifest.json"))
        print("built", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
