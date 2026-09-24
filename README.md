# Ontology → draw.io (Chowlk notation) generator

This repository contains:

- **`script/gen_drawio.py`** — converts any OWL/RDF ontology into a
  [draw.io](https://www.drawio.com/) / diagrams.net diagram drawn in the
  [**Chowlk** visual notation](https://chowlk.linkeddata.es/notation.html).
  The generated `.xml` opens directly in draw.io and round-trips back to an
  ontology through the [Chowlk converter](https://github.com/oeg-upm/Chowlk).
- **`examples/`** — a sample 50-class ontology and a real 29-class network
  ontology, plus the diagrams produced from each with every layout/routing
  option.
- **`references/`** — Chowlk notation notes.

The converter is **generic**: classes, properties, restrictions,
`equivalentClass` / `unionOf` / `intersectionOf` / `oneOf` / `complementOf`
axioms, disjointness, and named individuals are all discovered by traversing
the RDF graph — nothing is hard-coded per ontology.

This README covers running the generator **manually**, from a checkout of
this repo. If you're inside Claude Code, see
[Using this as a Claude Code skill](#using-this-as-a-claude-code-skill)
below — you likely don't need any of the manual setup at all.

---

## Using this as a Claude Code skill

This generator is also installed as the Claude Code skill
`ontology-to-drawio`. If you're working inside Claude Code, you don't need
to clone this repo, install anything, or run the CLI yourself — just ask,
e.g. "turn this ontology into a draw.io diagram" or "visualize
`foo.ttl`", and Claude invokes the skill directly.

Two differences from the manual instructions below, both by design:

- The skill's copy bundles `elkjs` under its own `scripts/node_modules/`, so
  `--layout elk` needs **no** `npm install` there — only Node.js itself.
  That's different from this repo, where `script/node_modules/` is
  gitignored (see [Requirements](#requirements)) and must be installed once.
- The skill decides layout/prerequisites for you (falling back to a mode
  that works if e.g. Node isn't available) rather than defaulting to
  `builtin` and leaving the choice to you.

This repo (`script/`, `references/`) is the **dev source** for that
installed skill — see `CLAUDE.md` for how the two are kept in sync
(`script/sync_skill.sh`) if you're working on the generator itself, not just
using it.

`dist/ontology-to-drawio.skill` is a packaged distributable (a zip archive
containing the skill's `SKILL.md`, `scripts/`, and `references/`, with
`elkjs` bundled) for installing this skill into a *different* Claude Code
setup: unzip it into that machine's `~/.claude/skills/` so it produces
`~/.claude/skills/ontology-to-drawio/`. Rebuild it after any generator
change with `script/sync_skill.sh` (`--check` reports whether it's stale
without changing anything).

---

## Requirements

| Feature | Needs |
| --- | --- |
| Base functionality (`--layout builtin`, the default) | Python 3.8+ and [`rdflib`](https://rdflib.dev/) |
| `--layout graphviz` / `--edge-routing graphviz` | [Graphviz](https://graphviz.org/) — the `dot` binary on your `PATH` |
| `--layout elk` | [Node.js](https://nodejs.org/) + a one-time `npm install` in `script/` |

### Install the Python dependency

```bash
# from the repo root
python3 -m venv venv
source venv/bin/activate
pip install rdflib
```

(This repo already contains a `venv/` with `rdflib` installed; if you use it,
just `source venv/bin/activate`.)

### Optional: Graphviz (for the `graphviz` options)

```bash
# macOS
brew install graphviz
# Debian/Ubuntu
sudo apt-get install graphviz
```

### Optional: ELK (for `--layout elk`)

Requires Node.js. Install the `elkjs` package once:

```bash
cd script
npm install
```

---

## Basic usage

```bash
python3 script/gen_drawio.py --input ontology.ttl --output diagram.xml
```

Then open `diagram.xml` in draw.io (**File → Open**, or drag it onto
<https://app.diagrams.net/>).

Short flags work too:

```bash
python3 script/gen_drawio.py -i ontology.owl -o diagram.xml
```

On success the script prints a summary (triple count, number of classes /
object properties / datatype properties / individuals / axioms) and confirms
the output XML is well-formed.

Every class box is automatically tinted by its top-level category — the
topmost `subClassOf` ancestor it descends from, regardless of namespace;
only categories with 2+ members get a color, singletons stay white — and a
color-key box on the diagram lists which color is which category, so the
tinting is identifiable rather than just "same color = related." Classes
with many incident relationships are outlined thicker as visual "hub"
anchors. Named individuals (the ABox) are drawn in their own grid band
below the class diagram, each with an `rdf:type` arrow to its class and
arrows for its object-property assertions to other individuals — kept out
of the class layout so an instance-heavy ontology can't distort it. Before
writing the output file, a fatal check scans for any real box overlap or
gap in a class's datatype-property stack, so a layout bug surfaces as a
clear error instead of a silently broken diagram.

---

## Command-line options

| Option | Values | Default | Description |
| --- | --- | --- | --- |
| `-i`, `--input` | path | *(required)* | The ontology file to read. |
| `-o`, `--output` | path | *(required)* | Where to write the draw.io XML. |
| `--format` | `turtle`, `xml`, `n3`, `nt`, `json-ld`, `trig` | auto-detected | rdflib parse format. Auto-detected from the file extension (see below) if omitted. |
| `--title` | text | derived | Title shown in the diagram's metadata note. Defaults to the ontology's `dcterms:title` / `rdfs:label`, falling back to the input filename. |
| `--layout` | `builtin`, `graphviz`, `elk` | `builtin` | How classes are positioned (see [Layout engines](#layout-engines)). |
| `--elk-mode` | `layered`, `stress` | `layered` | Only meaningful with `--layout elk` — see [Layout engines](#layout-engines). |
| `--edge-routing` | `straight`, `orthogonal`, `graphviz` | `straight` | How relationship lines are drawn (see [Edge routing](#edge-routing)). |

### `--format` auto-detection

If `--format` is omitted, it is guessed from the input file extension:

| Extension | Format |
| --- | --- |
| `.ttl`, `.turtle` | `turtle` |
| `.owl`, `.rdf`, `.xml` | `xml` (RDF/XML) |
| `.n3` | `n3` |
| `.nt`, `.ntriples` | `nt` |
| `.jsonld`, `.json` | `json-ld` |
| `.trig` | `trig` |
| *(anything else)* | `turtle` |

---

## Layout engines (`--layout`)

Controls where the class boxes are placed. All three produce the **same
Chowlk-valid diagram** — they differ only in readability of the arrangement.

### `builtin` (default)

The script's own layered / barycenter layout. No external tools required.
Compact and dependency-free; good for small ontologies.

### `graphviz`

Shells out to Graphviz's `dot` for a hierarchical layout — `subClassOf`
edges drive the top-to-bottom ranking, and related classes are pulled into
neighbouring columns by dot's crossing-minimization. Usually clearer than
`builtin` on medium/large ontologies. **Requires the `dot` binary.**

### `elk`

Shells out to the **Eclipse Layout Kernel** (`elkjs`) for a layered layout
with orthogonal edge routing that **reserves space for edge labels**, so
relationship names don't pile on top of one another. This is generally the
**most readable** option for dense ontologies with many labelled
relationships. **Requires Node.js + `cd script && npm install`.**

> `--layout elk` does its own orthogonal edge routing, so any `--edge-routing`
> value is ignored (the script prints a note if you pass one).

Under `--layout elk`, `--elk-mode {layered,stress}` picks between two ELK
algorithms. `layered` (default) is top-down hierarchical, best for
deep-taxonomy ontologies. `stress` is a force-directed layout for
ontologies that sprawl into a wide ribbon under `layered` — dense,
association-heavy, shallow hierarchies with many cross-links and few
subclass levels. See [When to ask for `--elk-mode
stress`](#when-to-ask-for---elk-mode-stress) below for concrete criteria.

---

## Edge routing (`--edge-routing`)

Controls how the relationship lines are drawn between boxes. (Ignored under
`--layout elk`, which routes edges itself.)

### `straight` (default)

Direct lines between shapes.

### `orthogonal`

Tags every edge so draw.io draws it as right-angle segments (draw.io's own
`edgeStyle=orthogonalEdgeStyle`, computed when the file is opened). Layout-
independent — works with any `--layout`. Note that draw.io's router only
bends an edge around its *own* two endpoints, not around classes in between.

### `graphviz`

Bakes in the actual **node-avoiding** edge paths Graphviz computes with
`splines=ortho`, emitting them as explicit draw.io waypoints, so long
relationship lines bend *around* intervening classes instead of cutting
through them.

> `--edge-routing graphviz` **requires `--layout graphviz`** — the waypoints
> come from the same `dot` run that positions the classes, so both must
> agree on coordinates.

---

## Which combination should I use?

**Class count matters more than anything else here.** Measured on synthetic
ontologies (pages are A4-equivalents at 96dpi; aspect is width/height, where
~1 is a square that fits a screen and >3 is a ribbon you scroll sideways):

| classes | `builtin` | `--layout elk` | `--layout elk --elk-mode stress` |
| --- | --- | --- | --- |
| 150 | 9pg, aspect 1.3, 0.1s | 151pg, aspect 3.1, 1s | 17pg, aspect 1.0, 6s |
| 300 | 18pg, aspect 1.4, 0.1s | 409pg, aspect 4.1, 2s | 34pg, aspect 1.0, 18s |
| 600 | 43pg, aspect 1.2, 0.2s | **1364pg, aspect 7.4**, 3s | 77pg, aspect 1.0, 73s |

- **Up to ~150 classes (the common case), best readability:** `--layout elk`.
- **No Node, or lines cutting through boxes:** `--layout graphviz --edge-routing graphviz`.
- **~150–400 classes, want a square drawing:** `--layout elk --elk-mode stress`.
- **Over ~400 classes:** `--layout builtin` — instant and compact. Or
  `--elk-mode stress` if you don't mind waiting a minute.
- **Quick look, zero external tools, any size:** `--layout builtin` (the default).

Two traps worth knowing:

- `--layout elk` (layered) puts each hierarchy level in a single row, so an
  ontology with a few hundred sibling classes becomes a ribbon. That is
  inherent to the algorithm — ELK's `wrapping` options target path-like
  graphs and do nothing for a wide hierarchy layer (verified directly
  against `elk_layout.js`). The `builtin` layout avoids it by wrapping wide
  layers into sub-rows, at a width that scales with the class count.
- `--elk-mode stress` costs roughly 6s at 150 classes, 18s at 300 and 73s at
  600, and prints nothing while it runs.

---

## When to ask for `--elk-mode stress`

Ask for it specifically (it is never the default) when:

- **A `--layout elk` (layered) diagram you already generated is a wide
  ribbon.** This is the most reliable trigger — if the printed diagram is
  clearly much wider than tall, or you can see it's several times wider
  than a screen, layered has hit the "each hierarchy level in one row"
  problem above and stress is the direct fix, not a tuning tweak.
- **The ontology's shape is flat and cross-linked, not deep.** Many classes
  with few `subClassOf` levels between them and many `owl:ObjectProperty`
  relationships criss-crossing between distant classes — layered has
  nothing to build a hierarchy from and sprawls.
- **Size is roughly 150–400 classes.** Below that, layered is already
  compact and stress adds several seconds for no shape benefit (see the
  table above). Above ~400, stress still works but costs a minute or more
  — `builtin` is the faster fallback at that size if you don't need a
  square-ish drawing.

Don't ask for it as a first try on a small or already-hierarchical
ontology — it won't look meaningfully different from `--layout elk`
(layered) there, just slower, and prints nothing while it runs so it can
look hung on a large one.

Edges in `--elk-mode stress` (and `builtin`/`graphviz`) route around a
class's own datatype-property boxes rather than through them: a
relationship or subclass arrow that would naturally connect to the bottom
of a class with attributes stacked underneath gets redirected off that
side and detoured through a clear column before approaching, instead of
cutting across the stack. `--layout elk` (layered) never needed this —
it routes through ELK's own port system instead.

---

## Examples

The `examples/` directory contains a synthetic 50-class ontology
(`university-ontology.ttl`) and the diagram produced from it with each
option, so you can compare them in draw.io:

```bash
# builtin layout, straight edges (default)
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-builtin.xml

# graphviz layout, straight edges
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-graphviz.xml \
  --layout graphviz

# graphviz layout, draw.io orthogonal edges
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-graphviz-orthogonal.xml \
  --layout graphviz --edge-routing orthogonal

# graphviz layout, baked-in node-avoiding waypoints
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-graphviz-waypoint.xml \
  --layout graphviz --edge-routing graphviz

# ELK layout (label-aware orthogonal routing)
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-elk.xml \
  --layout elk

# ELK stress layout (force-directed, best for dense/shallow-hierarchy ontologies)
python3 script/gen_drawio.py \
  -i examples/university-ontology.ttl \
  -o examples/university-diagram-elk-stress.xml \
  --layout elk --elk-mode stress
```

`examples/dflfn.ttl` (a real 29-class network ontology, with datatype
properties on most classes — useful for exercising the datatype-property-
stack routing described above) and its generated `dflfn.drawio.xml` at the
repo root are a second, non-synthetic example.

---

## Validating a generated diagram

There's no automated test suite — the ground-truth check is that the diagram
opens in draw.io and that the [Chowlk converter](https://github.com/oeg-upm/Chowlk)
can parse it back into an ontology without errors.

Run the bundled validator, which handles the clone/install/patch steps and
caches the checkout (`~/.cache/ontology-to-drawio/chowlk` by default) so
repeat runs are fast:

```bash
python3 script/validate_with_chowlk.py diagram.xml
```

Pass `--cache-dir DIR` to use a different cache location, or `--refresh` to
force a fresh clone (e.g. after a Chowlk upstream fix). Exit code 0 means a
clean pass; nonzero means `references/chowlk-notation-gotchas.md` is the
next place to look.

A clean run reports no errors (a "Base has not been declared" warning is
benign — it just means the diagram declares no default `@base` namespace).
The resulting `.ttl` output should be semantically equivalent regardless of
which `--layout` / `--edge-routing` was used, since those only affect
positioning, not the ontology content.

---

## Notes & limitations

- The output `.xml` is a draw.io diagram, **not** the Chowlk shape library —
  open it as a normal diagram, not via *Open Library*.
- Layout only affects readability; every option yields the same Chowlk-valid,
  semantically identical diagram.
- Size, not just density, should drive the `--layout` choice — see
  [Which combination should I use?](#which-combination-should-i-use); past
  a few hundred classes `--layout elk` (layered) degrades badly and
  `builtin` or `--elk-mode stress` are the better defaults.
- If a particular dense cluster still crowds under `elk`, the spacing
  constants in `gen_drawio.py` (`_compute_layout_elk`'s `layoutOptions`,
  or the `STRESS_*` constants for `--elk-mode stress`) can be tuned —
  they were seeded from formulas/measurements, not eyeballed in a real
  draw.io renderer (this repo's dev environment has none), so a real
  ontology may want real tuning.
- Relationship/subclass edges route around a class's own datatype-property
  stack (see [When to ask for `--elk-mode
  stress`](#when-to-ask-for---elk-mode-stress)), but only around that
  class's *own* stack — the detour doesn't check every other class in the
  diagram, so in a dense layout it can still occasionally clip a nearby,
  unrelated class's stack if that class happens to sit right where the
  detour routes through. Rare in practice (verified zero such collisions
  on the ~30-class ontology this was tuned against) but not structurally
  ruled out.
