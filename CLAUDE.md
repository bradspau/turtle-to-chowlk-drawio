# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`script/gen_drawio.py` — a generator that converts any OWL/RDF ontology into a Chowlk-notation draw.io diagram (Python 3.8+ + `rdflib`; optional Graphviz `dot` and/or Node+elkjs for alternate layouts). Full setup, CLI flags, and validation steps are documented in `README.md` — read that before working on the generator rather than duplicating it here.

**This repo is the dev source for the installed Claude Code skill `ontology-to-drawio`** (`~/.claude/skills/ontology-to-drawio/`). `script/` and `references/` here are byte-identical to that skill's `scripts/`/`references/`. After changing the generator, the validator, or `references/chowlk-notation-gotchas.md`, run **`script/sync_skill.sh`** — it copies the changed files into the installed skill and repackages `skill/ontology-to-drawio.skill`. `script/sync_skill.sh --check` reports drift without writing anything.

Two things that script deliberately does *not* touch: `SKILL.md` is authored in the skill directory (there is no copy in this repo, so it is only ever packaged, never overwritten), and no `CLAUDE.md` is packaged into the distributable — this file describes *this repo*, and a copy of it inside the skill directory would misdescribe the skill (wrong paths, dangling `README.md` reference).

It has no build system beyond `script/sync_skill.sh`, no linter, and no automated test suite — verification is running the generator and round-tripping the output through the real Chowlk converter (see `README.md`'s "Validating a generated diagram").

## Working with the generator

- Before hand-editing a shape's connection/label behavior, check `references/chowlk-notation-gotchas.md` — it documents real Chowlk-parser quirks (exact edge styles/labels required, geometry-only attachment for datatype-property boxes, which shape styles get parsed as metadata) discovered by round-tripping diagrams through the actual Chowlk converter, not just the notation spec.
- To validate a diagram the generator produced, run the bundled `script/validate_with_chowlk.py` (clones and caches the real Chowlk converter) — see `README.md`'s "Validating a generated diagram" section for details.
