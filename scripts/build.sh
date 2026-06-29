#!/usr/bin/env bash
# Netlify / local build command for the Impression Editions website.
#
#   1. Install Python dependencies
#   2. Generate Hugo content + assets from the Impression-Editions GitHub org
#   3. Build the static site with Hugo
#
# Requires: python3, pip, hugo (extended). Reads GITHUB_TOKEN from the env.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Installing Python dependencies"
python3 -m pip install --quiet -r requirements.txt

echo "==> Generating catalog from GitHub"
python3 build_catalog.py

echo "==> Building Hugo site"
hugo --minify

echo "==> Done"
