# Chowlk notation gotchas

Things that are true about how Chowlk's parser (`app/source/chowlk/model/diagram_model.py` and friends) actually classifies shapes and edges, discovered by generating diagrams and running them through the real parser rather than by reading the notation spec alone. Read this when `validate_with_chowlk.py` reports an error/warning and you need to figure out which style string is wrong.

## Edges

- **Plain `rdfs:subClassOf` must be UNLABELED**, with `endArrow=block;endFill=0`. A *labeled* block arrow is not recognized as anything — Chowlk only reads the default subclass axiom off an unlabeled hollow-triangle arrow.
- **`equivalentClass` / `disjointWith` need `endArrow=classic`** plus a label that is literally the predicate string (`owl:equivalentClass`, `owl:disjointWith`). Chowlk's `edge_types` matches on that exact substring in the label text, not on style alone.
- A **restriction arrow's label grammar is one bracket pair**: `(some) prop`, `(all) prop`, `(value) prop`, `(0..1) prop` for a plain subclass restriction. When combined with an outer class axiom, both keywords go inside the *same* pair: `(eq some) prop` — never `eq (some) prop`. Chowlk's `relation_restriction()` regex expects exactly one `(...)` containing `<axiom> <restriction>` together.
- Object-property edges carry `fontColor=#000099` (Chowlk's notation blue) and vary by whether domain/range are asserted: solid = both asserted, dashed = neither, filled/hollow circle at the tail = only domain/only range.
- Unqualified cardinality restrictions are read off the **arrow itself**, not a floating text box — Chowlk resolves `owl:onClass`/`owl:onDataRange` from the arrow's *target* for qualified cardinality, so a real target class is required there. For unqualified cardinality (no meaningful target), self-loop on the subject rather than inventing a relationship, and keep `dashed=1` so Chowlk doesn't also misread the arrow as a domain/range assertion.
- Cardinality range grammar: `owl:cardinality N` → `(N..N)`, `owl:minCardinality N` → `(N..N)` with N as the upper bound(!), `owl:maxCardinality N` → `(0..N)`. Qualified variants use square brackets `[N..N]` and name the class. Chowlk special-cases the literal string `"N"` (not `*`) to mean "no upper bound" — anything else non-numeric throws `not a number`.
- `owl:complementOf`'s stencil has one fixed edge style (`endArrow=open;dashed=1`, labeled `owl:complementOf`) independent of any wrapping class axiom — don't combine it with an outer `verb_label`.

## Boxes and geometry

- **Datatype-property boxes attach to their class purely by geometry**: the box's top-left corner must sit within 5px of the class box's bottom-left corner (`classify_boxes_into_classes_and_datatype_properties`). There is no connecting edge — drawing one is actively wrong, since an edge with no recognized arrow type is reported as a parse error.
- Datatype-property restrictions (hasValue, cardinality) are rendered the *same way* — geometrically stacked in the same column, no edge, just appended below the ordinary datatype properties.
- A `hasValue` literal on a datatype property restriction **must carry an explicit `^^datatype` suffix** — Chowlk's `datatype_property_restriction()` unconditionally splits the text on `^^` and indexes into the result. A literal with no datatype in the source ontology will crash the parser with an `IndexError` unless you default it to `^^xsd:string`.
- A blank-node member of an `intersectionOf`/`unionOf` (an "anonymous class") must be an otherwise-empty, unlabeled box — its restriction is a datatype-property box geometrically stacked underneath it, exactly like a named class's own restrictions.

## Metadata boxes — position doesn't matter, style does

Chowlk classifies these by style substring, anywhere on the canvas:
- `shape=document` → parsed as **ontology metadata**, keyed by `prefix:suffix: value` lines (or `owl:Ontology: <uri>`). Free-form prose here gets misparsed as bogus metadata triples.
- `shape=note` → parsed as **namespace declarations**, one `prefix: <uri>` per line. Nothing else is safe to put in a note box.
- Anything else (a legend, a caption) must avoid `shape=note`, `shape=document`, `ellipse`, `hexagon`, `rhombus`, `edgeLabel`/`text`, and `rounded` in its style — any of those substrings gets it misclassified as ontology data instead of being skipped as inert.

## Interpreting `validate_with_chowlk.py` output

- `"There is no errors."` plus `Warning Base: A base has not been declared. The first namespace has been taken as base` is a **clean pass** — that warning just means the source ontology (or the diagram) never asserted an explicit base IRI, which is harmless.
- You'll also see `SyntaxWarning: invalid escape sequence '\>'` / `'\('` printed from `chowlk/resources/utils.py` — that's Chowlk's own regex strings using backslash sequences that Python 3.12+ warns about at import time. It's noise, not a diagram problem; ignore it.
- Any other warning or error means a shape or edge style doesn't match what Chowlk's classifier expects. The error-annotated XML written to `<diagram>.chowlk-errors.xml` marks the offending cells — open it in draw.io to see exactly which box/arrow triggered it, then cross-reference the style substring against this file or `diagram_model.py` in the cached checkout (`~/.cache/ontology-to-drawio/chowlk` by default).
