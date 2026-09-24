#!/usr/bin/env bash
# Sync this repo's generator into the installed Claude Code skill, then
# repackage the distributable.
#
# CLAUDE.md notes that script/ and references/ must be kept byte-identical
# to ~/.claude/skills/ontology-to-drawio/ by hand, and that there is no
# automation for it. This is that automation -- run it after any change to
# the generator, the validator, or the notation reference.
#
# Usage: script/sync_skill.sh [--check]
#   --check  report drift and exit nonzero instead of copying anything
#            (for confirming the installed skill matches the repo).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${SKILL_DIR:-$HOME/.claude/skills/ontology-to-drawio}"
DIST="$REPO_ROOT/skill/ontology-to-drawio.skill"

# repo path -> path inside the skill
FILES=(
  "script/gen_drawio.py:scripts/gen_drawio.py"
  "script/validate_with_chowlk.py:scripts/validate_with_chowlk.py"
  "script/elk_layout.js:scripts/elk_layout.js"
  "script/package.json:scripts/package.json"
  "script/package-lock.json:scripts/package-lock.json"
  "references/chowlk-notation-gotchas.md:references/chowlk-notation-gotchas.md"
)

check_only=0
[ "${1:-}" = "--check" ] && check_only=1

drift=0
for pair in "${FILES[@]}"; do
  src="$REPO_ROOT/${pair%%:*}"
  dst="$SKILL_DIR/${pair##*:}"
  [ -f "$src" ] || { echo "missing in repo: $src" >&2; exit 1; }
  if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
    drift=1
    if [ "$check_only" = 1 ]; then
      echo "DRIFT: ${pair##*:}"
    else
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
      echo "synced: ${pair##*:}"
    fi
  fi
done

if [ "$check_only" = 1 ]; then
  [ "$drift" = 0 ] && echo "installed skill matches the repo" && exit 0
  echo "installed skill is out of date -- run script/sync_skill.sh" >&2
  exit 1
fi

chmod +x "$SKILL_DIR/scripts/validate_with_chowlk.py"
[ "$drift" = 0 ] && echo "scripts already in sync"

# SKILL.md is authored in the skill directory, not the repo, so it is never
# overwritten here -- it is only packaged.
mkdir -p "$REPO_ROOT/skill"
rm -f "$DIST"
(cd "$(dirname "$SKILL_DIR")" && zip -qr "$DIST" "$(basename "$SKILL_DIR")" \
  -x '*/CLAUDE.md' -x '*/.DS_Store' -x '*/__pycache__/*' -x '*.pyc')
echo "packaged: ${DIST#"$REPO_ROOT/"} ($(du -h "$DIST" | cut -f1))"
