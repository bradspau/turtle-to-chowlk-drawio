#!/usr/bin/env python3
"""
gen_drawio.py -- Generic OWL/RDF ontology -> draw.io (Chowlk notation) converter.

Given any ontology file (Turtle, RDF/XML, N3, JSON-LD, NTriples -- anything
rdflib can parse), generates a draw.io diagram using the Chowlk visual
notation (https://chowlk.linkeddata.es/notation). The mxCell styles used
match those in drawio/chowlk-library-complete.xml.

This is NOT specific to any one ontology: classes, properties, restrictions,
equivalentClass/unionOf/intersectionOf/oneOf/complementOf axioms, and
AllDisjointClasses/AllDisjointProperties are all discovered generically via
rdflib graph traversal -- nothing is hardcoded by class or property name.

Requires: rdflib (pip install rdflib)

Usage:
    python3 gen_drawio.py --input ontology.ttl --output diagram.xml
    python3 gen_drawio.py -i ontology.owl -o diagram.xml --format xml
    python3 gen_drawio.py -i ontology.ttl -o diagram.xml --format xml

Options:
    -i, --input   Path to the ontology file (required)
    -o, --output  Path to write the draw.io XML (required)
    --format      rdflib parse format: turtle (default), xml, n3, nt,
                  json-ld, trig -- auto-detected from file extension if
                  omitted
    --title       Diagram title shown in the metadata note (default:
                  derived from the ontology's dcterms:title / rdfs:label,
                  falling back to the input filename)
    --layout      Layout engine used to position classes: 'builtin'
                  (default), 'graphviz' (shells out to `dot`; requires
                  Graphviz on PATH), or 'elk' (shells out to the Eclipse
                  Layout Kernel for layered layout with orthogonal,
                  label-aware edge routing; requires Node.js +
                  `cd script && npm install`)
    --edge-routing  Edge drawing style: 'straight' (default),
                  'orthogonal' (right-angle segments, routed by drawio
                  itself when the file is opened), or 'graphviz' (bakes in
                  Graphviz's node-avoiding splines=ortho edge paths as
                  explicit waypoints; requires --layout graphviz)
"""
import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zlib
from collections import defaultdict, deque

try:
    import rdflib
    from rdflib import RDF, RDFS, OWL, BNode, URIRef, Literal
except ImportError:
    sys.exit(
        "rdflib is required. Install it with:\n"
        "  uv venv .venv && source .venv/bin/activate && uv pip install rdflib\n"
        "or: pip install rdflib"
    )

DC = rdflib.Namespace("http://purl.org/dc/elements/1.1/")
DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(
        description="Convert an OWL/RDF ontology into a draw.io diagram using Chowlk notation."
    )
    ap.add_argument("-i", "--input", required=True, help="Path to the ontology file")
    ap.add_argument("-o", "--output", required=True, help="Path to write the draw.io XML")
    ap.add_argument(
        "--format", default=None,
        help="rdflib parse format (turtle, xml, n3, nt, json-ld, trig). "
             "Auto-detected from the file extension if omitted.",
    )
    ap.add_argument(
        "--title", default=None,
        help="Diagram title for the metadata note (default: derived from the ontology)",
    )
    ap.add_argument(
        "--layout", choices=["builtin", "graphviz", "elk"], default="builtin",
        help="Layout engine used to position classes: 'builtin' (default) is "
             "this script's own layered/barycenter layout; 'graphviz' shells "
             "out to Graphviz's `dot` for hierarchical layout (requires the "
             "`dot` binary on PATH, e.g. `brew install graphviz`); 'elk' "
             "shells out to the Eclipse Layout Kernel (elkjs) for a layered "
             "layout with orthogonal edge routing that reserves space for "
             "edge labels -- the most readable option on dense ontologies "
             "with many labelled relationships (requires Node.js and a "
             "one-time `cd script && npm install`). 'elk' produces its own "
             "orthogonal waypoints, so --edge-routing is ignored with it.",
    )
    ap.add_argument(
        "--elk-mode", choices=["layered", "stress"], default="layered",
        help="Only meaningful with --layout elk. 'layered' (default) is a "
             "top-down hierarchical layout, best for deep-taxonomy "
             "ontologies. 'stress' is a force-directed layout for dense, "
             "association-heavy, shallow-hierarchy ontologies that sprawl "
             "into a wide, hard-to-read ribbon under 'layered'.",
    )
    ap.add_argument(
        "--edge-routing", choices=["straight", "orthogonal", "graphviz"], default="straight",
        help="Edge drawing style: 'straight' (default) draws direct lines "
             "between shapes; 'orthogonal' routes edges as right-angle "
             "segments (drawio's own edgeStyle=orthogonalEdgeStyle, computed "
             "by drawio itself when the file is opened); 'graphviz' bakes in "
             "the actual node-avoiding edge paths Graphviz computes with "
             "splines=ortho, emitting them as explicit drawio waypoints so "
             "long relationship lines bend AROUND intervening classes "
             "instead of cutting through them. 'graphviz' requires "
             "--layout graphviz (the waypoints come from the same `dot` run "
             "that positions the classes, so both must agree).",
    )
    return ap.parse_args()


FORMAT_BY_EXT = {
    ".ttl": "turtle",
    ".turtle": "turtle",
    ".n3": "n3",
    ".nt": "nt",
    ".ntriples": "nt",
    ".owl": "xml",
    ".rdf": "xml",
    ".xml": "xml",
    ".jsonld": "json-ld",
    ".json": "json-ld",
    ".trig": "trig",
}


def guess_format(path):
    for ext, fmt in FORMAT_BY_EXT.items():
        if path.lower().endswith(ext):
            return fmt
    return "turtle"


# ---------------------------------------------------------------------------
# Load the ontology
# ---------------------------------------------------------------------------

def load_graph(path, fmt):
    g = rdflib.Graph()
    g.parse(path, format=fmt or guess_format(path))
    return g


def make_qname_fn(g):
    """Return (qname_fn, used_prefixes) where qname_fn renders any URI as
    prefix:local (minting a short auto-prefix for any namespace rdflib
    doesn't already know), and used_prefixes accumulates every prefix -> ns
    URI pair actually emitted, for display in the diagram's namespace note."""
    auto_prefixes = {}
    counter = [0]
    used_prefixes = {}

    def qname(uri):
        if not isinstance(uri, URIRef):
            return str(uri)
        try:
            prefix, ns, local = g.namespace_manager.compute_qname(uri, generate=True)
            if prefix == "":
                prefix = "_base"
            used_prefixes[prefix] = str(ns)
            return f"{prefix}:{local}"
        except Exception:
            # fall back to a minted prefix per namespace
            s = str(uri)
            if "#" in s:
                ns, local = s.rsplit("#", 1)
                ns += "#"
            elif "/" in s:
                ns, local = s.rsplit("/", 1)
                ns += "/"
            else:
                return s
            if ns not in auto_prefixes:
                counter[0] += 1
                auto_prefixes[ns] = f"ns{counter[0]}"
            prefix = auto_prefixes[ns]
            used_prefixes[prefix] = ns
            return f"{prefix}:{local}"

    return qname, used_prefixes


# ---------------------------------------------------------------------------
# Generic OWL traversal helpers
# ---------------------------------------------------------------------------

def is_list(g, node):
    return (node, RDF.first, None) in g


def rdf_list(g, node):
    items = []
    seen = set()
    while node and node != RDF.nil and node not in seen:
        seen.add(node)
        first = g.value(node, RDF.first)
        if first is not None:
            items.append(first)
        node = g.value(node, RDF.rest)
    return items


def count_dp_restriction_boxes(g, node, data_prop_qnames, qname, _seen=None):
    """Conservatively count how many geometrically-stacked datatype-property
    restriction boxes a class-expression tree will need once rendered (used
    only to reserve enough row height in compute_layout -- overcounting a
    restriction that ends up as an edge instead of a box is harmless; a
    per-class UNDERcount would cause real box overlaps)."""
    if _seen is None:
        _seen = set()
    if not isinstance(node, BNode) or node in _seen:
        return 0
    _seen.add(node)
    total = 0
    on_prop = g.value(node, OWL.onProperty)
    if on_prop is not None:
        if qname(on_prop) in data_prop_qnames:
            total += 1
        return total
    for pred in (OWL.intersectionOf, OWL.unionOf):
        lst = g.value(node, pred)
        if lst is not None:
            for m in rdf_list(g, lst):
                total += count_dp_restriction_boxes(g, m, data_prop_qnames, qname, _seen)
    return total


def count_annex_hub_boxes(g, node, data_prop_qnames, qname, _seen=None):
    """Mirror of ExpressionRenderer's actual box-adding behaviour (see
    render_as_class_link/_render_settish/_render_member/_render_oneof), used
    only to reserve enough row height in compute_layout for the hub/member
    boxes an equivalentClass or subClassOf expression will need once
    rendered -- WITHOUT actually rendering it. A top-level owl:Restriction
    (someValuesFrom/allValuesFrom/hasValue/cardinality on an object property)
    renders as a plain edge with no box at all, or (for a datatype property)
    as a stacked box already counted by count_dp_restriction_boxes -- either
    way it contributes 0 here. Only intersectionOf/unionOf/oneOf actually add
    hub/member boxes to the anchor's own column."""
    if _seen is None:
        _seen = set()
    if not isinstance(node, BNode) or node in _seen:
        return 0
    _seen.add(node)

    if g.value(node, OWL.onProperty) is not None:
        return 0  # top-level restriction: edge or dp-stacked box, no hub box

    def member_boxes(m):
        if not isinstance(m, BNode):
            return 0  # named class: just an edge to its existing box
        on_prop = g.value(m, OWL.onProperty)
        if on_prop is not None:
            n = 1  # the member's own blank box
            is_dp = qname(on_prop) in data_prop_qnames
            if is_dp and (g.value(m, OWL.hasValue) is not None
                          or g.value(m, OWL.someValuesFrom) is not None
                          or g.value(m, OWL.allValuesFrom) is not None):
                n += 1  # its stacked datatype-restriction box
            return n
        inter = g.value(m, OWL.intersectionOf)
        union = g.value(m, OWL.unionOf)
        members = rdf_list(g, inter) if inter is not None else (rdf_list(g, union) if union is not None else None)
        if members is not None:
            return 1 + sum(member_boxes(sm) for sm in members)  # sub-hub + its members
        return 0

    inter = g.value(node, OWL.intersectionOf)
    union = g.value(node, OWL.unionOf)
    oneof = g.value(node, OWL.oneOf)
    if inter is not None:
        return 1 + sum(member_boxes(m) for m in rdf_list(g, inter))
    if union is not None:
        return 1 + sum(member_boxes(m) for m in rdf_list(g, union))
    if oneof is not None:
        members = rdf_list(g, oneof)
        return 1 + len(members)  # hub + one box per enumerated member
    return 0  # complementOf, or unrecognized shape: plain edge or nothing


def expr_referenced_classes(g, node, _seen=None):
    """Yield every named class (URIRef) referenced anywhere inside an
    owl:Restriction / intersectionOf / unionOf / complementOf expression
    tree -- e.g. someValuesFrom/allValuesFrom/hasValue/onClass targets, or
    named intersectionOf/unionOf members. Used only to feed the graphviz
    layout extra "soft" edges so classes an equivalentClass/subClassOf
    expression actually connects (which render_as_class_link will draw a
    real edge or hub-edge to) get pulled into nearby columns instead of
    landing arbitrarily far apart and producing a long cross-diagram line."""
    if _seen is None:
        _seen = set()
    if isinstance(node, URIRef):
        yield node
        return
    if not isinstance(node, BNode) or node in _seen:
        return
    _seen.add(node)
    for pred in (OWL.someValuesFrom, OWL.allValuesFrom, OWL.hasValue, OWL.onClass):
        v = g.value(node, pred)
        if isinstance(v, URIRef):
            yield v
    for pred in (OWL.intersectionOf, OWL.unionOf):
        lst = g.value(node, pred)
        if lst is not None:
            for m in rdf_list(g, lst):
                yield from expr_referenced_classes(g, m, _seen)
    comp = g.value(node, OWL.complementOf)
    if comp is not None:
        yield from expr_referenced_classes(g, comp, _seen)


def label_of(g, qname, node):
    for pred in (RDFS.label,):
        v = g.value(node, pred)
        if v is not None:
            return str(v)
    return qname(node)


def literal_repr(lit):
    if isinstance(lit, Literal):
        dt = lit.datatype
        if dt:
            dtq = str(dt).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            return f'"{lit}"^^{dtq}'
        return f'"{lit}"'
    return str(lit)


def literal_repr_for_dp_box(lit):
    """Like literal_repr, but always includes a ^^datatype suffix.
    Chowlk's datatype_property_restriction() unconditionally splits the
    hasValue text on '^^' (anonymousClass.py: aux = ...split('^^'); aux[1])
    -- a literal with no explicit datatype in the source ontology would
    otherwise crash the parser with an IndexError."""
    if isinstance(lit, Literal):
        dt = lit.datatype
        if dt:
            dtq = str(dt).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            return f'"{lit}"^^{dtq}'
        return f'"{lit}"^^xsd:string'
    return str(lit)


# ---------------------------------------------------------------------------
# Extract classes, properties, restrictions
# ---------------------------------------------------------------------------

class OntologyModel:
    def __init__(self, g, qname):
        self.g = g
        self.qname = qname
        self.classes = {}          # qname -> {uri, label, subClassOf: [qname], is_external}
        self.class_uris = {}       # qname -> URIRef
        self.obj_props = {}        # qname -> dict
        self.data_props = {}       # qname -> dict
        self.individuals = {}      # qname -> {uri, types: [qname]}
        self.individual_relations = []     # [(subject_qname, property_qname, object_qname)]
        self.disjoint_class_sets = []      # [[qname,...]]
        self.disjoint_property_sets = []   # [[qname,...]]
        self.equivalent_class_exprs = defaultdict(list)  # class_qname -> [expr]
        self.subclass_restriction_exprs = defaultdict(list)  # class_qname -> [expr] (anonymous restrictions used as subClassOf)
        self._extract()

    def _extract(self):
        g, qname = self.g, self.qname

        # classes (named only)
        class_subjects = set(g.subjects(RDF.type, OWL.Class))
        # Iterate a sorted copy, not the bare set: Python hashes URIRefs
        # differently per process (PYTHONHASHSEED), so iterating the set
        # directly makes class emission order -- and therefore every
        # downstream layout position -- nondeterministic across separate
        # runs of the same input. `class_subjects` itself stays a set for
        # the O(1) membership tests below.
        for s in sorted(class_subjects, key=str):
            if isinstance(s, BNode):
                continue
            cq = qname(s)
            self.classes[cq] = {
                "uri": s,
                "label": label_of(g, qname, s),
                "subClassOf": [],
            }
            self.class_uris[cq] = s

        # rdfs:subClassOf (named parent vs. blank-node restriction)
        for s, o in g.subject_objects(RDFS.subClassOf):
            if s not in class_subjects or isinstance(s, BNode):
                continue
            cq = qname(s)
            if cq not in self.classes:
                continue
            if isinstance(o, URIRef):
                self.classes[cq]["subClassOf"].append(qname(o))
            elif isinstance(o, BNode):
                self.subclass_restriction_exprs[cq].append(o)

        # equivalentClass
        for s, o in g.subject_objects(OWL.equivalentClass):
            if s not in class_subjects or isinstance(s, BNode):
                continue
            cq = qname(s)
            if cq not in self.classes:
                continue
            self.equivalent_class_exprs[cq].append(o)

        # object properties
        for s in g.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(s, BNode):
                continue
            pq = qname(s)
            self.obj_props[pq] = {
                "uri": s,
                "label": label_of(g, qname, s),
                "domain": qname(g.value(s, RDFS.domain)) if g.value(s, RDFS.domain) and isinstance(g.value(s, RDFS.domain), URIRef) else None,
                "range": qname(g.value(s, RDFS.range)) if g.value(s, RDFS.range) and isinstance(g.value(s, RDFS.range), URIRef) else None,
                "inverseOf": qname(g.value(s, OWL.inverseOf)) if g.value(s, OWL.inverseOf) else None,
                "subPropertyOf": qname(g.value(s, RDFS.subPropertyOf)) if g.value(s, RDFS.subPropertyOf) and isinstance(g.value(s, RDFS.subPropertyOf), URIRef) else None,
                "transitive": (s, RDF.type, OWL.TransitiveProperty) in g,
                "functional": (s, RDF.type, OWL.FunctionalProperty) in g,
                "symmetric": (s, RDF.type, OWL.SymmetricProperty) in g,
            }

        # datatype properties
        for s in g.subjects(RDF.type, OWL.DatatypeProperty):
            if isinstance(s, BNode):
                continue
            pq = qname(s)
            domains = [qname(d) for d in g.objects(s, RDFS.domain) if isinstance(d, URIRef)]
            rang = g.value(s, RDFS.range)
            self.data_props[pq] = {
                "uri": s,
                "label": label_of(g, qname, s),
                "domains": domains,
                "range": qname(rang) if isinstance(rang, URIRef) else None,
            }

        # named individuals
        for s in sorted(g.subjects(RDF.type, OWL.NamedIndividual), key=str):
            if isinstance(s, BNode):
                continue
            iq = qname(s)
            types = [qname(t) for t in g.objects(s, RDF.type)
                     if isinstance(t, URIRef) and t != OWL.NamedIndividual]
            self.individuals[iq] = {"uri": s, "types": types}

        # object-property assertions BETWEEN two named individuals
        # (:alice :knows :bob). Only assertions whose predicate is a modeled
        # object property and whose object is another named individual are
        # kept -- anything else is either a datatype assertion or points
        # outside the set of individuals this diagram draws.
        for iq, ind in self.individuals.items():
            for p, o in g.predicate_objects(ind["uri"]):
                if not isinstance(o, URIRef) or not isinstance(p, URIRef):
                    continue
                pq, oq = qname(p), qname(o)
                if pq in self.obj_props and oq in self.individuals:
                    self.individual_relations.append((iq, pq, oq))

        # AllDisjointClasses / AllDisjointProperties
        for s in g.subjects(RDF.type, OWL.AllDisjointClasses):
            members_node = g.value(s, OWL.members)
            if members_node:
                members = [qname(m) for m in rdf_list(g, members_node) if isinstance(m, URIRef)]
                if len(members) >= 2:
                    self.disjoint_class_sets.append(members)
        for s in g.subjects(RDF.type, OWL.AllDisjointProperties):
            members_node = g.value(s, OWL.members)
            if members_node:
                members = [qname(m) for m in rdf_list(g, members_node) if isinstance(m, URIRef)]
                if len(members) >= 2:
                    self.disjoint_property_sets.append(members)

        # pairwise disjointWith (as a degenerate 2-member set, deduped)
        seen_pairs = set()
        for s, o in g.subject_objects(OWL.disjointWith):
            if isinstance(s, BNode) or isinstance(o, BNode):
                continue
            a, b = qname(s), qname(o)
            key = tuple(sorted([a, b]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            self.disjoint_class_sets.append([a, b])


# ---------------------------------------------------------------------------
# Generic expression walker: renders owl:Restriction / intersectionOf /
# unionOf / oneOf / complementOf blank-node trees into Chowlk hub-and-edge
# fragments, without any per-ontology-specific knowledge.
# ---------------------------------------------------------------------------

# Chowlk recognises a labeled arrow as a class axiom ONLY when its text
# literally contains one of these predicate strings (see edge_types in
# diagram_model.py); anything else on a labeled arrow is parsed as an
# owl:ObjectProperty relation instead. verb_label keys used throughout this
# module ("eq", "dis", "") map onto them here.
CLASS_AXIOM_PREDICATES = {
    "eq": "owl:equivalentClass",
    "dis": "owl:disjointWith",
    "": "rdfs:subClassOf",
}


class ExpressionRenderer:
    """Walks an arbitrary OWL class-expression blank node and emits Chowlk
    diagram fragments (hub vertices + dashed diamondThin edges) generically."""

    EQUIV_HUB_STYLE = "ellipse;whiteSpace=wrap;html=1;aspect=fixed;fontSize=17;"
    INTERSECTION_LABEL = "⨅"   # ⨅
    UNION_LABEL = "⨆"          # ⨆
    ONEOF_STYLE = "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;fixedSize=1;"
    HUB_EDGE = "endArrow=diamondThin;endSize=12;html=1;endFill=0;dashed=1;"
    HASVALUE_STYLE = "rounded=0;whiteSpace=wrap;html=1;dashed=1;fillColor=#ffe6cc;strokeColor=#d79b00;"
    RESTRICTION_LABEL_STYLE = "text;html=1;align=center;verticalAlign=middle;resizable=0;points=[];labelBackgroundColor=#ffffff;"

    def __init__(self, model, diagram):
        self.model = model
        self.diagram = diagram  # DiagramBuilder, for add_vertex/add_edge + class_ids + a place-near-anchor helper

    @staticmethod
    def _class_axiom_edge(verb_label):
        """Return (style, label) for a direct class-axiom edge. The default
        rdfs:subClassOf axiom is only recognised UNLABELED with endArrow=block
        (an unlabeled endArrow=classic edge is "Could not recognize type of
        arrow"); equivalentClass/disjointWith need endArrow=classic plus the
        literal predicate string as the label."""
        if verb_label in CLASS_AXIOM_PREDICATES and verb_label:
            return (
                "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;",
                CLASS_AXIOM_PREDICATES[verb_label],
            )
        return (
            "endArrow=block;html=1;fontColor=#000099;endFill=0;endSize=10;arcSize=0;rounded=0;",
            None,
        )

    def render_as_class_link(self, subject_qname, node, verb_label):
        """Render `node` (a blank-node class expression, or a named class)
        as a direct labeled edge from `subject_qname`'s box, tagged with
        `verb_label` (a key into CLASS_AXIOM_PREDICATES: "eq" for
        equivalentClass, "dis" for disjointWith, "" for plain subClassOf).

        Verified against the real Chowlk parser: a single labeled edge whose
        text is the literal predicate string ("owl:equivalentClass" etc, or
        no label at all for the rdfs:subClassOf default) from the class
        directly to a named class / hexagon (oneOf) / ellipse (intersectionOf,
        unionOf) / blank restriction node is ALL that's needed -- Chowlk's
        writer reads relation["target"] against concepts/hexagons/
        anonymous_concepts/anonymous_classes to build the right triple.
        No wrapping "≡"/"⊥" ellipse-of-two-members is required (and using one
        with a labeled edge is actively wrong: add_relation_to_ellipse()
        rejects any arrow out of an ellipse whose type isn't
        ellipse_connection/owl:ObjectProperty/owl:complementOf).
        """
        g, qname = self.model.g, self.model.qname
        d = self.diagram

        edge_style, class_axiom_label = self._class_axiom_edge(verb_label)

        if isinstance(node, URIRef):
            target_q = qname(node)
            if subject_qname in d.class_ids and target_q in d.class_ids:
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[target_q],
                    edge_style, label=class_axiom_label,
                )
            return

        if not isinstance(node, BNode):
            return

        # owl:Restriction
        on_prop = g.value(node, OWL.onProperty)
        if on_prop is not None:
            self._render_restriction(subject_qname, node, on_prop, verb_label)
            return

        # owl:intersectionOf / unionOf / oneOf / complementOf
        inter = g.value(node, OWL.intersectionOf)
        union = g.value(node, OWL.unionOf)
        oneof = g.value(node, OWL.oneOf)
        comp = g.value(node, OWL.complementOf)

        if inter is not None:
            self._render_settish(subject_qname, rdf_list(g, inter), self.INTERSECTION_LABEL, verb_label)
        elif union is not None:
            self._render_settish(subject_qname, rdf_list(g, union), self.UNION_LABEL, verb_label)
        elif oneof is not None:
            self._render_oneof(subject_qname, rdf_list(g, oneof), verb_label)
        elif comp is not None:
            self._render_complement(subject_qname, comp, verb_label)
        else:
            # unrecognized blank-node expression shape; skip silently
            pass

    @staticmethod
    def _restriction_label(verb_label, restriction_kw, prop_q, suffix=""):
        """Build a Chowlk restriction-arrow label. When verb_label is the
        default ("", i.e. plain subClassOf) only the restriction keyword is
        needed: "(some) prop". For a non-default class axiom (equivalentClass/
        disjointWith) BOTH keywords must be combined inside one set of
        parentheses -- "(eq some) prop", not "eq (some) prop" -- because
        Chowlk's relation_restriction() regex expects exactly one bracket
        pair containing "<axiom> <restriction>" together (diagram_model.py)."""
        if verb_label:
            return f"({verb_label} {restriction_kw}) {prop_q}{suffix}"
        return f"({restriction_kw}) {prop_q}{suffix}"

    def _render_restriction(self, subject_qname, node, on_prop, verb_label):
        g, qname = self.model.g, self.model.qname
        d = self.diagram
        prop_q = qname(on_prop)
        is_datatype_prop = prop_q in self.model.data_props

        some_from = g.value(node, OWL.someValuesFrom)
        all_from = g.value(node, OWL.allValuesFrom)
        has_value = g.value(node, OWL.hasValue)
        # capture the SPECIFIC cardinality predicate so exact/min/max are not
        # conflated (they map to different Chowlk ranges: N..N / N..* / 0..N)
        exact_c = g.value(node, OWL.cardinality)
        min_c = g.value(node, OWL.minCardinality)
        max_c = g.value(node, OWL.maxCardinality)
        exact_qc = g.value(node, OWL.qualifiedCardinality)
        min_qc = g.value(node, OWL.minQualifiedCardinality)
        max_qc = g.value(node, OWL.maxQualifiedCardinality)
        card = exact_c or min_c or max_c
        qcard = exact_qc or min_qc or max_qc
        on_class = g.value(node, OWL.onClass)
        on_datarange = g.value(node, OWL.onDataRange)

        if some_from is not None and isinstance(some_from, URIRef) and qname(some_from) in d.class_ids:
            target_q = qname(some_from)
            if subject_qname in d.class_ids:
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[target_q],
                    "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;dashed=1;",
                    label=self._restriction_label(verb_label, "some", prop_q),
                    label_style=self.RESTRICTION_LABEL_STYLE,
                )
            return

        if all_from is not None and isinstance(all_from, URIRef) and qname(all_from) in d.class_ids:
            target_q = qname(all_from)
            if subject_qname in d.class_ids:
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[target_q],
                    "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;",
                    label=self._restriction_label(verb_label, "all", prop_q),
                    label_style=self.RESTRICTION_LABEL_STYLE,
                )
            return

        if has_value is not None:
            if is_datatype_prop:
                val_repr = literal_repr_for_dp_box(has_value) if isinstance(has_value, Literal) else qname(has_value)
                # Chowlk datatype-property restrictions live INSIDE a
                # geometrically-stacked attribute box (same placement as a
                # plain datatype property) -- there is no separate edge.
                self.diagram.add_dp_restriction_box(subject_qname, f"(value) {prop_q}: {val_repr}")
            elif subject_qname in d.class_ids and isinstance(has_value, URIRef) and qname(has_value) in self.model.individuals:
                # hasValue on an object property targets a named individual
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[qname(has_value)],
                    "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;",
                    label=self._restriction_label(verb_label, "value", prop_q),
                    label_style=self.RESTRICTION_LABEL_STYLE,
                )
            return

        if card is not None or qcard is not None:
            # Chowlk cardinality range grammar (N1..N2). Chowlk special-cases
            # the literal string "N" (not "*") to mean "no upper bound", and
            # "0" to mean "no lower bound" (check_cardinality_restriction);
            # any other non-numeric token throws "not a number".
            #   owl:cardinality N        -> (N..N)
            #   owl:minCardinality N     -> (N..N)   [N as the upper bound]
            #   owl:maxCardinality N     -> (0..N)
            # qualified variants use square brackets [N1..N2] and name the class.
            if exact_c is not None or exact_qc is not None:
                n = exact_c if exact_c is not None else exact_qc
                rng = f"{n}..{n}"
            elif min_c is not None or min_qc is not None:
                n = min_c if min_c is not None else min_qc
                rng = f"{n}..N"
            else:
                n = max_c if max_c is not None else max_qc
                rng = f"0..{n}"

            if is_datatype_prop:
                # Datatype-property cardinality: text lives in the
                # geometrically-stacked attribute box, e.g. "prop (0..1)".
                self.diagram.add_dp_restriction_box(subject_qname, f"{prop_q} ({rng})")
                return

            # Object-property cardinality: a real edge is required (Chowlk
            # reads min/max/exact cardinality off the ARROW, not a floating
            # box). For qualified cardinality, Chowlk resolves owl:onClass /
            # owl:onDataRange from the arrow's TARGET (not from text), so a
            # real target class is required there; unqualified cardinality
            # ignores the target entirely, so we self-loop on the subject to
            # avoid inventing a fake relationship. "dashed=1" keeps Chowlk
            # from also reading this arrow as an (undashed) domain/range
            # assertion.
            onto = on_class or on_datarange
            target_id = subject_qname
            card_text = f"[{rng}]" if qcard is not None else f"({rng})"
            if qcard is not None and isinstance(onto, URIRef) and qname(onto) in d.class_ids:
                target_id = qname(onto)
            # combined class-axiom+restriction notation requires the
            # cardinality range in ITS OWN bracket pair nested inside the
            # outer (axiom ...) one, e.g. "(eq (0..1)) prop"
            label = f"({verb_label} {card_text}) {prop_q}" if verb_label else f"{prop_q} {card_text}"
            if subject_qname in d.class_ids and target_id in d.class_ids:
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[target_id],
                    "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;dashed=1;",
                    label=label, label_style=self.RESTRICTION_LABEL_STYLE,
                )
            return

        # restriction present but of a shape we don't specifically render
        # (e.g. hasSelf) -- render as a geometrically-stacked note so nothing
        # silently vanishes and no unrecognized floating box is introduced
        self.diagram.add_dp_restriction_box(subject_qname, f"(restriction) {prop_q}")

    def _render_settish(self, subject_qname, members, hub_symbol, verb_label):
        d = self.diagram
        hub_id = d.add_near(subject_qname, 60, hub_symbol, self.EQUIV_HUB_STYLE, w=30, h=30, is_hub=True)
        if subject_qname in d.class_ids:
            # Direct labeled edge from the class to the intersectionOf/unionOf
            # ellipse -- verified against the real Chowlk parser to produce
            # the exact correct "owl:equivalentClass [ owl:intersectionOf (...) ]"
            # triple with zero errors/warnings. No wrapping "≡"/"⊥" ellipse is
            # needed (or wanted) here.
            edge_style, class_axiom_label = self._class_axiom_edge(verb_label)
            d.add_edge(d.class_ids[subject_qname], hub_id, edge_style, label=class_axiom_label)
        for m in members:
            self._render_member(subject_qname, hub_id, m)

    def _render_member(self, anchor_qname, hub_id, m):
        g, qname = self.model.g, self.model.qname
        d = self.diagram
        if isinstance(m, URIRef):
            mq = qname(m)
            if mq in d.class_ids:
                d.add_edge(hub_id, d.class_ids[mq], self.HUB_EDGE)
            return
        if not isinstance(m, BNode):
            return
        on_prop = g.value(m, OWL.onProperty)
        if on_prop is not None:
            prop_q = qname(on_prop)
            is_datatype_prop = prop_q in self.model.data_props
            has_value = g.value(m, OWL.hasValue)
            some_from = g.value(m, OWL.someValuesFrom)
            all_from = g.value(m, OWL.allValuesFrom)
            # A blank restriction node that is a member of intersectionOf/
            # unionOf is an "anonymous class" in Chowlk -- it must be an
            # otherwise-empty (unlabeled) box, and its restriction is read
            # from a datatype-property attribute box GEOMETRICALLY STACKED
            # underneath it (no edge). This mirrors exactly how a named
            # class's own restrictions attach.
            blank_id = d.add_near(anchor_qname, 60, "", d.CLASS_STYLE, w=60, h=30)
            d.add_edge(hub_id, blank_id, self.HUB_EDGE)
            if has_value is not None and is_datatype_prop:
                val_repr = literal_repr_for_dp_box(has_value) if isinstance(has_value, Literal) else qname(has_value)
                d.add_stacked_box_under(anchor_qname, blank_id, f"(value) {prop_q}: {val_repr}", self.HASVALUE_STYLE)
            elif some_from is not None and is_datatype_prop:
                target = qname(some_from) if isinstance(some_from, URIRef) else str(some_from)
                d.add_stacked_box_under(anchor_qname, blank_id, f"(some) {prop_q}: {target}", self.HASVALUE_STYLE)
            elif all_from is not None and is_datatype_prop:
                target = qname(all_from) if isinstance(all_from, URIRef) else str(all_from)
                d.add_stacked_box_under(anchor_qname, blank_id, f"(all) {prop_q}: {target}", self.HASVALUE_STYLE)
            elif has_value is not None and qname(has_value) in d.class_ids:
                # hasValue on an object property: the blank node's restriction
                # is instead an arrow FROM the blank box to the individual.
                d.add_edge(blank_id, d.class_ids[qname(has_value)], "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;", label=f"(value) {prop_q}", label_style=self.RESTRICTION_LABEL_STYLE)
            elif some_from is not None and isinstance(some_from, URIRef) and qname(some_from) in d.class_ids:
                d.add_edge(blank_id, d.class_ids[qname(some_from)], "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;dashed=1;", label=f"(some) {prop_q}", label_style=self.RESTRICTION_LABEL_STYLE)
            elif all_from is not None and isinstance(all_from, URIRef) and qname(all_from) in d.class_ids:
                d.add_edge(blank_id, d.class_ids[qname(all_from)], "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;", label=f"(all) {prop_q}", label_style=self.RESTRICTION_LABEL_STYLE)
            return
        # nested intersection/union
        inter = g.value(m, OWL.intersectionOf)
        union = g.value(m, OWL.unionOf)
        if inter is not None:
            sub_hub = d.add_near(anchor_qname, 60, self.INTERSECTION_LABEL, self.EQUIV_HUB_STYLE, w=30, h=30, is_hub=True)
            d.add_edge(hub_id, sub_hub, self.HUB_EDGE)
            for sm in rdf_list(g, inter):
                self._render_member(anchor_qname, sub_hub, sm)
        elif union is not None:
            sub_hub = d.add_near(anchor_qname, 60, self.UNION_LABEL, self.EQUIV_HUB_STYLE, w=30, h=30, is_hub=True)
            d.add_edge(hub_id, sub_hub, self.HUB_EDGE)
            for sm in rdf_list(g, union):
                self._render_member(anchor_qname, sub_hub, sm)

    def _render_oneof(self, subject_qname, members, verb_label):
        d = self.diagram
        label = "«owl:oneOf»"
        hub_id = d.add_near(subject_qname, 60, label, self.ONEOF_STYLE, w=150, h=30, is_hub=True)
        if subject_qname in d.class_ids:
            edge_style, class_axiom_label = self._class_axiom_edge(verb_label)
            d.add_edge(d.class_ids[subject_qname], hub_id, edge_style, label=class_axiom_label)
        for m in members:
            mq = self.model.qname(m) if isinstance(m, URIRef) else str(m)
            box_id = d.add_near(subject_qname, 260, f"<u>{mq}</u>", self.diagram.CLASS_STYLE, w=150, h=30)
            d.add_edge(hub_id, box_id, self.HUB_EDGE)

    def _render_complement(self, subject_qname, comp_node, verb_label):
        d = self.diagram
        if isinstance(comp_node, URIRef):
            target_q = self.model.qname(comp_node)
            if subject_qname in d.class_ids and target_q in d.class_ids:
                # The "complement of" stencil's single fixed edge style is
                # endArrow=open;dashed=1 (bidirectional/hollow), labeled with
                # the literal "owl:complementOf" keyword -- this is a fixed
                # relation type independent of any wrapping class axiom, so
                # verb_label (subClassOf/equivalentClass/disjointWith on the
                # OUTER relation, if any) is intentionally not combined here.
                d.add_edge(
                    d.class_ids[subject_qname], d.class_ids[target_q],
                    "endArrow=open;html=1;fontColor=#000099;endFill=0;dashed=1;",
                    label="owl:complementOf",
                    label_style=self.RESTRICTION_LABEL_STYLE,
                )


# ---------------------------------------------------------------------------
# Diagram builder: mxCell emission + generic layout
# ---------------------------------------------------------------------------

class DiagramBuilder:
    CLASS_STYLE = (
        "rounded=0;whiteSpace=wrap;html=1;snapToPoint=1;points=[[0.1,0],[0.2,0],[0.3,0],[0.4,0],"
        "[0.5,0],[0.6,0],[0.7,0],[0.8,0],[0.9,0],[0,0.1],[0,0.3],[0,0.5],[0,0.7],[0,0.9],[0.1,1],"
        "[0.2,1],[0.3,1],[0.4,1],[0.5,1],[0.6,1],[0.7,1],[0.8,1],[0.9,1],[1,0.1],[1,0.3],[1,0.5],"
        "[1,0.7],[1,0.9]];"
    )
    EXTERNAL_CLASS_STYLE = CLASS_STYLE + "fillColor=#dae8fc;strokeColor=#6c8ebf;"
    # Pastel palette for category tinting (Task: category tinting). Avoids
    # every other semantically-meaningful color already used elsewhere in
    # this file, so a tinted class is never visually confused with one of
    # them: DP yellow (#fff2cc/#d6b656), external-class blue
    # (#dae8fc/#6c8ebf), hasValue/cardinality restriction orange
    # (#ffe6cc/#d79b00, see HASVALUE_STYLE), and the legend box's own gray
    # (#f5f5f5/#666666, see emit_notes).
    CATEGORY_PALETTE = [
        ("#d5e8d4", "#82b366"),  # green
        ("#d0cee2", "#56517e"),  # slate/lavender
        ("#e1d5e7", "#9673a6"),  # purple
        ("#f8cecc", "#b85450"),  # red
        ("#b0e3e6", "#0e8088"),  # teal
        ("#bac8d3", "#23445d"),  # blue-gray
    ]
    # A category (top-level subClassOf ancestor) with fewer members than
    # this stays plain white -- coloring a singleton just adds visual noise
    # without grouping anything.
    MIN_CATEGORY_SIZE = 2
    # A class this well-connected is a natural navigation anchor in the
    # diagram -- outlining it thicker draws the eye there first, the same
    # way a hub node would stand out in a hand-drawn diagram.
    HUB_HIGHLIGHT_DEGREE = 7
    # -- --elk-mode stress tuning. Seeded from the exact formulas
    # howtocreate.md's Sec 5 documents (calibrated there on a ~73-class
    # ontology); NOT eyeball-validated against a rendered diagram in this
    # environment (no in-container draw.io renderer -- see README.md's
    # "Validating a generated diagram" section). If a real diagram looks too
    # cramped or too sprawling in draw.io, these are one-line adjustments. --
    STRESS_EDGE_LEN_BASE = 220     # px "resting" length for a non-subclass edge before scaling
    STRESS_DEGREE_REF = 4          # degree at which the stretch factor is 1.0 (no stretch)
    STRESS_DEGREE_FACTOR = 0.12    # stretch added per degree above STRESS_DEGREE_REF
    STRESS_DEGREE_LEN_CAP = 2.8    # maximum stretch multiplier regardless of degree
    STRESS_SCALE_REF = 73          # class count the base spacing above was tuned against
    STRESS_SCALE_MIN = 1.0
    STRESS_SCALE_MAX = 1.8
    # Chowlk datatype-property boxes: SOLID when both domain and range are
    # asserted (`domain and range (DP)` stencil), DASHED when the range is
    # missing (`no domain no range (DP)` stencil). The yellow fill is a
    # readability embellishment on top of the base stencil (which has no fill).
    DP_STYLE_TYPED = CLASS_STYLE + "fillColor=#fff2cc;strokeColor=#d6b656;"
    DP_STYLE_UNTYPED = CLASS_STYLE + "dashed=1;fillColor=#fff2cc;strokeColor=#d6b656;"

    # Exit/entry constraints are appended per-edge in emit_subclass_edges
    # (via _side_point), not baked in here -- a parent class with its own
    # datatype-property stack needs its south entry redirected, and that
    # can only be decided per parent, not once for the whole notation.
    SUBCLASS_EDGE_STYLE = (
        "endArrow=block;html=1;fontColor=#000099;endFill=0;endSize=10;arcSize=0;rounded=0;"
    )
    # Chowlk relation arrows carry fontColor=#000099 (the notation's blue).
    OP_EDGE_STYLE = {
        (True, True): "endArrow=classic;html=1;fontColor=#000099;endSize=8;arcSize=0;rounded=0;",
        (False, False): "endArrow=classic;html=1;fontColor=#000099;endSize=8;dashed=1;arcSize=0;rounded=0;",
        (True, False): "endArrow=classic;html=1;fontColor=#000099;endSize=8;startArrow=oval;startFill=1;arcSize=0;rounded=0;",
        (False, True): "endArrow=classic;html=1;fontColor=#000099;endSize=8;startArrow=oval;startFill=0;arcSize=0;rounded=0;",
    }
    OP_LABEL_STYLE_SOLID = "text;html=1;align=center;verticalAlign=middle;resizable=0;points=[];labelBackgroundColor=#ffffff;"
    OP_LABEL_STYLE_DASHED = "edgeLabel;html=1;align=center;verticalAlign=middle;resizable=0;points=[];"
    DISJOINT_HUB_STYLE = "ellipse;whiteSpace=wrap;html=1;aspect=fixed;fontSize=17;"
    HUB_EDGE = "endArrow=diamondThin;endSize=12;html=1;endFill=0;dashed=1;"

    # Chowlk detects an individual purely by an UNDERLINED label -- its
    # classifier tests `"fontStyle=4" in style or "<u" in value` BEFORE it
    # ever checks for a box shape (see diagram_model.py's element dispatch),
    # so an individual is an ordinary class-shaped box whose value is wrapped
    # in <u>...</u>. The purple fill is a readability embellishment only.
    INDIVIDUAL_STYLE = CLASS_STYLE + "fillColor=#e6d0de;strokeColor=#996185;"
    # rdf:type link, individual -> class. Chowlk matches the edge label as a
    # SUBSTRING against its edge_types list, so the label must contain the
    # literal "rdf:type"; the notation writes it as <<rdf:type>>.
    RDF_TYPE_EDGE_STYLE = (
        "endArrow=open;html=1;fontColor=#000099;endFill=0;dashed=1;endSize=8;"
    )
    # Entity-encoded on purpose: with html=1 draw.io would swallow a literal
    # "<<rdf:type>>" as an unknown tag, so the stored value carries the
    # entities (matching the shape library's own stencil). Chowlk strips
    # tags, not entities, so its substring test still sees "rdf:type".
    RDF_TYPE_LABEL = "&lt;&lt;rdf:type&gt;&gt;"
    RDF_TYPE_LABEL_STYLE = (
        "text;html=1;align=center;verticalAlign=middle;resizable=0;points=[];"
        "fontColor=#000000;labelBackgroundColor=#ffffff;"
    )
    # Object-property assertion between two individuals: a plain classic
    # arrow labelled with the property qname (no domain/range circles --
    # those describe the property's schema, not an instance assertion).
    INDIVIDUAL_RELATION_EDGE_STYLE = "endArrow=classic;html=1;endSize=8;"

    # Layout of the individuals band drawn beneath the class diagram.
    INDIV_W, INDIV_H = 190, 30
    INDIV_COL_GAP, INDIV_ROW_GAP = 240, 70
    INDIV_BAND_GAP = 160      # vertical clearance below the lowest class content
    INDIV_MAX_COLS = 8

    CLASS_W, CLASS_H = 190, 40
    DP_W, DP_H = 190, 26
    # Vertical gap between stacked datatype-property boxes (and between a
    # class and its first one). Zero: Chowlk's actual attachment rule
    # (verified against diagram_model.py's classify_boxes_into_classes_and_
    # datatype_properties) only requires each box's top-left corner within
    # 5px of the box directly above it -- there's no reason NOT to make the
    # whole stack flush, and a visible gap here reads as a broken layout
    # rather than a single attribute block.
    DP_STACK_GAP = 0
    # COL_GAP leaves a gutter of COL_GAP - CLASS_W between adjacent boxes in a
    # row for object-property edges (and their labels) to route through. 260
    # (a 70px gutter) turned out too tight on ontologies with heavy
    # cross-linking -- edges and labels routinely overlapped neighbouring
    # class names. 340 (150px) gives routing room without making a row of
    # MAX_ROW_COLS classes unreasonably wide.
    COL_GAP = 340
    # A hierarchy layer with no children/parents of its own (e.g. Protege's
    # root layer, which is where every class without an asserted superclass
    # lands) can easily hold 20+ classes -- laid out on one row that's a
    # single unbroken horizontal line just as unreadable as the old vertical
    # stacks it replaced. Wrap any layer wider than this into multiple
    # sub-rows, preserving the barycenter left-to-right order so connected
    # classes are still adjacent.
    MAX_ROW_COLS = 6
    # ...but 6 is only the right width for a SMALL ontology. A fixed wrap
    # width makes the drawing grow in one direction only, so a 600-class
    # ontology comes out 1900x16400px -- a ribbon you scroll forever. The
    # total area is almost independent of the wrap width (measured: 31-40
    # Mpx across widths 6-25 on a 600-class ontology); only the aspect
    # ratio changes. So scale the width with the square root of the class
    # count to keep the drawing roughly square, and keep MAX_ROW_COLS as
    # the floor so small ontologies are untouched. The coefficient is set
    # so that this reproduces exactly 6 at ~50 classes (the size the fixed
    # value was originally tuned against) and gives ~20 at 600, which
    # measured at aspect 1.15.
    ROW_COLS_PER_SQRT_CLASS = 0.8
    LAYER_GAP = 140    # vertical gap between distinct hierarchy layers
    SUBROW_GAP = 90    # vertical gap between wrapped sub-rows of the same layer

    def __init__(self, model, edge_routing="straight", layout="builtin", elk_mode="layered"):
        self.model = model
        self._categories, self._category_colors = self._compute_categories()
        self._degrees = self._compute_degrees()
        # Precomputed once, before any layout runs, so _side_point (used by
        # both object-property and subclass edges) can tell -- regardless of
        # emission order -- whether a class will end up with a datatype-
        # property/annex stack below it. Every layout method already
        # computes this same pair locally for its own row/node sizing; this
        # is the one copy shared by edge-anchoring, which runs independently
        # of layout and, for object properties, before emit_data_properties.
        _dp_stack_count = self._compute_dp_stack_counts()
        _annex_height = self._compute_annex_stack_heights()
        self._stack_heights = {
            cq: self._class_block_height(cq, _dp_stack_count, _annex_height)
            for cq in model.classes
        }
        self.cells = []
        self._vertex_rects = []            # [(cell_id, x, y, w, h, label), ...]
        self._dp_box_rects = defaultdict(list)  # class_qname -> [(x, y, w, h), ...]
        self._id_counter = 100
        self.class_ids = {}
        self.positions = {}
        self.row_bottom = {}   # class_qname -> y-coordinate of the bottom of its datatype-property stack
        self.annex_cursor_y = defaultdict(float)  # class_qname -> next free y-offset within the annex column
        self.dp_stack_counts = defaultdict(int)   # class_qname -> number of DP/restriction boxes stacked so far
        self._hub_coords = {}
        self._anchor_slot = defaultdict(int)  # class_qname -> next connection-point slot to hand out (see _next_anchor_frac)
        # drawio's own mxGraph client computes the actual routed path at
        # render/open time -- "orthogonal" here just tags every edge with
        # edgeStyle=orthogonalEdgeStyle so drawio bends it in right-angle
        # segments (respecting each edge's exit/entry constraints, see
        # _anchor_style) instead of drawing a straight diagonal line. This is
        # purely a style-string prefix added once in add_edge, so it doesn't
        # touch Chowlk's own parsing (Chowlk's edge-type detection only ever
        # substring-matches things like "dashed=1"/"startArrow=oval", never
        # the edgeStyle key -- verified against its diagram_model.py).
        # The ELK layout is inseparable from its own orthogonal, label-aware
        # edge routing, so it always drives edge geometry regardless of
        # --edge-routing (see main()'s guard). drawio's own orthogonal style
        # is therefore only used when NOT under ELK.
        self.elk_layout = layout == "elk"
        self.elk_mode = elk_mode if self.elk_layout else "layered"
        self._elk_stress = self.elk_layout and self.elk_mode == "stress"
        self.orthogonal_edges = (
            edge_routing == "orthogonal" and not self.elk_layout
        ) or self._elk_stress
        # Both "graphviz" routing and the ELK layout bake real, node-avoiding
        # paths into the edges as explicit drawio waypoints -- instead of
        # asking drawio to route (which only ever bends around an edge's OWN
        # two endpoints, never the classes in between, the root cause of
        # lines cutting through boxes). The layout captures each engine's
        # computed path and stores it here, keyed by (source_class_qname,
        # target_class_qname); add_edge then emits those points. Populated by
        # _compute_layout_graphviz / _compute_layout_elk.
        self.waypoint_routing = edge_routing == "graphviz" or (self.elk_layout and not self._elk_stress)
        self.edge_waypoints = defaultdict(list)   # (tail_q, head_q) -> [ [(x,y),...], ... ]
        self._class_qname_by_id = {}              # reverse of class_ids, filled in emit_classes
        self.individual_ids = {}                  # individual_qname -> cell id, filled in emit_individuals

    # -- low-level cell emission -----------------------------------------

    def next_id(self):
        self._id_counter += 1
        return f"c{self._id_counter}"

    @staticmethod
    def esc(s):
        return html.escape(str(s), quote=True)

    def add_vertex(self, x, y, w, h, value, style, cell_id=None):
        cid = cell_id or self.next_id()
        self.cells.append(
            f'<mxCell id="{cid}" value="{self.esc(value)}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        self._vertex_rects.append((cid, x, y, w, h, str(value)))
        return cid

    # style keys that pin an edge to a fixed perimeter point on its endpoints
    # (see _anchor_style / SUBCLASS_EDGE_STYLE). In waypoint mode these are
    # stripped: Graphviz already chose where the edge leaves/enters each box,
    # so forcing a different fixed exit/entry would kink the baked-in path.
    _ANCHOR_KEY_RE = re.compile(r"(?:exit|entry)[XY]=[^;]*;|(?:exit|entry)D[xy]=[^;]*;")

    def _consume_waypoints(self, source_id, target_id):
        """Pop one Graphviz-computed routing for the class pair drawn from
        source_id to target_id, returning a list of (x, y) drawio points (or
        None). Tries the pair as-is, then reversed -- a subclass edge is fed
        to `dot` as parent->child but drawn child->parent, so its stored
        routing is found under the reversed key and returned tail-to-head
        reversed to match the drawn direction."""
        sq = self._class_qname_by_id.get(source_id)
        tq = self._class_qname_by_id.get(target_id)
        if sq is None or tq is None:
            return None
        routings = self.edge_waypoints.get((sq, tq))
        if routings:
            return routings.pop(0)
        routings = self.edge_waypoints.get((tq, sq))
        if routings:
            return list(reversed(routings.pop(0)))
        return None

    def add_edge(self, source_id, target_id, style, label=None, label_style=None, extra_points=None):
        cid = self.next_id()
        geometry = '<mxGeometry relative="1" as="geometry"/>'
        if self.orthogonal_edges:
            style = "edgeStyle=orthogonalEdgeStyle;rounded=0;" + style
        elif self.waypoint_routing:
            pts = self._consume_waypoints(source_id, target_id)
            if pts:
                # let drawio attach the endpoints to the boxes and just follow
                # the baked-in interior bends -- drop dot's first/last points
                # (they sit on the node perimeter, which drawio derives) and
                # strip the fixed exit/entry anchors that would fight them.
                interior = pts[1:-1] if len(pts) > 2 else []
                if interior:
                    style = self._ANCHOR_KEY_RE.sub("", style)
                    pts_xml = "".join(f'<mxPoint x="{x:.1f}" y="{y:.1f}"/>' for x, y in interior)
                    geometry = (
                        f'<mxGeometry relative="1" as="geometry">'
                        f'<Array as="points">{pts_xml}</Array></mxGeometry>'
                    )
        if extra_points and geometry == '<mxGeometry relative="1" as="geometry"/>':
            # Explicit bend(s) alongside fixed exit/entry constraints --
            # unlike the waypoint_routing branch above, these don't replace
            # the anchors, they just force the path clear of a specific
            # obstacle (see _stack_clearance_point). Standard mxGraph: a
            # source/target with fixed connection points can still carry
            # intermediate points the router must pass through.
            pts_xml = "".join(f'<mxPoint x="{x:.1f}" y="{y:.1f}"/>' for x, y in extra_points)
            geometry = (
                f'<mxGeometry relative="1" as="geometry">'
                f'<Array as="points">{pts_xml}</Array></mxGeometry>'
            )
        self.cells.append(
            f'<mxCell id="{cid}" value="" style="{style}" edge="1" source="{source_id}" '
            f'target="{target_id}" parent="1">{geometry}</mxCell>'
        )
        if label:
            lstyle = label_style or self.OP_LABEL_STYLE_SOLID
            lid = self.next_id()
            if self.elk_layout:
                # ELK laid the graph out already KNOWING each label's size and
                # reserving space for it (see _compute_layout_elk), so the
                # edges are spread far enough that a label sitting dead-centre
                # on its own routed path has room. No stagger needed -- and
                # staggering would only drag the label off the clear lane ELK
                # opened for it. (Placement is still an edge-relative x/offset,
                # the one geometry drawio renders reliably; ELK's exact
                # absolute label point isn't reproducible in that model.)
                along, perp = 0.0, 0
            else:
                # Otherwise every label defaulting to the same relative
                # midpoint makes labels merge into unreadable stacks wherever
                # several edges pass through one region. Stagger each label's
                # position along its edge and its perpendicular pixel offset,
                # deterministically from a hash of the label text (not
                # randomly -- the same ontology should always render the same
                # diagram).
                h = zlib.crc32(label.encode("utf-8"))
                along = round(-0.3 + 0.6 * ((h % 997) / 997), 3)
                perp = (h // 997) % 31 - 15
            self.cells.append(
                f'<mxCell id="{lid}" value="{self.esc(label)}" style="{lstyle}" vertex="1" '
                f'connectable="0" parent="{cid}"><mxGeometry x="{along}" relative="1" as="geometry">'
                f'<mxPoint y="{perp}" as="offset"/></mxGeometry></mxCell>'
            )
        return cid

    # -- helpers used by ExpressionRenderer for placing satellite boxes ---
    #
    # equivalentClass / restriction annex content (hubs, hasValue boxes,
    # oneOf enumerations, disjointness hubs, etc.) is stacked directly
    # beneath the anchor class's OWN column, continuing on from its
    # datatype-property boxes -- the exact same column, never off to the
    # side. An earlier version placed this content in a lane measured from
    # the RIGHT EDGE OF THE ANCHOR'S WHOLE ROW: harmless for a lone class in
    # its own row, but for a class sitting in, say, column 1 of a 6-wide row
    # it put the hub past column 6 -- a long, mostly-horizontal dashed line
    # cutting across every unrelated class in between just to reach a
    # satellite box, for no reason other than which row the anchor happened
    # to land in. Reserving the extra height up front in compute_layout (see
    # _compute_annex_stack_heights) and stacking in-column instead keeps that
    # edge as short as the datatype-property boxes' own (invisible,
    # geometry-only) attachment to their class.

    def add_near(self, anchor_qname, dx, value, style, w, h, is_hub=False):
        """Place a new box directly beneath `anchor_qname`'s own column
        (after its datatype-property stack), stacking vertically on repeated
        calls. `dx` is unused -- kept for call-site compatibility."""
        ax, ay = self.positions.get(anchor_qname, (0, 0))
        dp_height = self.dp_stack_counts[anchor_qname] * (self.DP_H + self.DP_STACK_GAP)
        y = ay + self.CLASS_H + dp_height + self.annex_cursor_y[anchor_qname]
        self.annex_cursor_y[anchor_qname] += (h + 20)
        cid = self.add_vertex(ax, y, w, h, value, style)
        self._hub_coords[cid] = (ax, y)
        return cid

    def add_vertex_hub(self, x, y, w, h, value, style):
        cid = self.add_vertex(x, y, w, h, value, style)
        self._hub_coords[cid] = (x, y)
        return cid

    def add_dp_restriction_box(self, anchor_qname, value):
        """Append one more box to `anchor_qname`'s geometrically-stacked
        datatype-property column (continuing on from its ordinary datatype
        properties). Chowlk reads a datatype-property RESTRICTION exactly
        like a plain datatype property -- same placement, same lack of any
        connecting edge -- so this reuses the same stack counter and column
        as emit_data_properties()."""
        idx = self.dp_stack_counts[anchor_qname]
        x, y = self.positions[anchor_qname]
        dpx, dpy = x, y + self.CLASS_H + idx * (self.DP_H + self.DP_STACK_GAP)
        cid = self.add_vertex(dpx, dpy, self.DP_W, self.DP_H, value, self.DP_STYLE_UNTYPED)
        self._dp_box_rects[anchor_qname].append((dpx, dpy, self.DP_W, self.DP_H))
        self.dp_stack_counts[anchor_qname] += 1
        return cid

    def add_stacked_box_under(self, anchor_qname, above_id, value, style):
        """Stack a new box directly beneath `above_id` (touching corner-to-
        corner), for the anonymous-class-with-a-datatype-restriction pattern:
        a blank member of an intersectionOf/unionOf needs its restriction
        attribute box geometrically stacked underneath it, with no edge.
        `above_id` is always one of this module's 30px-tall "blank boxes"
        (see _render_member), so its height is fixed rather than looked up.
        Also advances anchor_qname's annex cursor so the NEXT add_near() call
        for this anchor doesn't overlap the space this stacked box just
        occupied (add_near already advanced the cursor for the blank box
        itself, but has no way to know a second box was stacked under it)."""
        ax, ay = self._hub_coords.get(above_id, (0, 0))
        y = ay + 30
        cid = self.add_vertex(ax, y, 220, 30, value, style)
        self._hub_coords[cid] = (ax, y)
        self.annex_cursor_y[anchor_qname] += 50
        return cid

    # -- layered (Sugiyama-style) hierarchical layout ---------------------
    #
    # Classes are placed by:
    #   1) layer assignment: each class's layer = longest path from a
    #      subClassOf root (roots = layer 0, their children = layer 1, ...).
    #      Classes with no subclass edges are layered by BFS distance along
    #      object-property domain/range edges from whatever they connect to,
    #      so isolated-but-related classes still land near their neighbours
    #      instead of in an arbitrary bucket.
    #   2) crossing reduction: a barycenter heuristic run for a few passes --
    #      each class is repositioned within its layer at the mean column of
    #      the classes it's connected to in the adjacent layer, then ties are
    #      broken by the previous ordering to keep the result stable.
    #   3) coordinate assignment: column -> x, layer -> y (row height still
    #      grows to fit any stacked datatype-property boxes). A layer wider
    #      than MAX_ROW_COLS wraps into multiple sub-rows rather than one
    #      arbitrarily wide row, keeping barycenter order intact.
    #
    # This replaces a plain N-column grid (which ignored the actual subclass/
    # relation edges entirely and produced arbitrary-looking vertical stacks)
    # with a drawing where parents sit above their children and connected
    # classes cluster together, the way graphviz `dot` or yFiles' hierarchic
    # layout would draw an ontology.

    def _wrap_cols(self):
        """How many classes go in one sub-row before wrapping. See
        ROW_COLS_PER_SQRT_CLASS for why this scales with the class count."""
        n = len(self.model.classes)
        return max(self.MAX_ROW_COLS,
                   int(round(self.ROW_COLS_PER_SQRT_CLASS * math.sqrt(n))))

    def compute_layout(self, layout="builtin"):
        if layout == "graphviz":
            return self._compute_layout_graphviz()
        if layout == "elk":
            return self._compute_layout_elk()
        return self._compute_layout_builtin()

    # -- shared: how many datatype-property / restriction boxes stack under
    # each class, so both layout engines can reserve row/node height to fit
    # them without colliding with whatever comes below --

    def _compute_dp_stack_counts(self):
        model = self.model
        dp_stack_count = defaultdict(int)
        for p in model.data_props.values():
            for dom in p["domains"]:
                dp_stack_count[dom] += 1
        data_prop_qnames = set(model.data_props.keys())
        for cq, exprs in model.subclass_restriction_exprs.items():
            for expr in exprs:
                dp_stack_count[cq] += count_dp_restriction_boxes(model.g, expr, data_prop_qnames, model.qname)
        for cq, exprs in model.equivalent_class_exprs.items():
            for expr in exprs:
                dp_stack_count[cq] += count_dp_restriction_boxes(model.g, expr, data_prop_qnames, model.qname)
        return dp_stack_count

    # A hub/member box in the equivalentClass/disjoint "annex" (see add_near)
    # is a 30px-tall box plus a 20px gap between boxes -- same constant
    # add_near itself uses for its vertical cursor.
    ANNEX_SLOT_H = 50

    def _compute_annex_stack_heights(self):
        """How much extra vertical space (in px) each class's equivalentClass
        / subClassOf-restriction hub-trees and disjointClasses membership
        will need, stacked directly beneath its datatype-property column (see
        add_near) -- so both layout engines can reserve enough row height for
        it up front, the same way _compute_dp_stack_counts already does for
        plain datatype properties."""
        model = self.model
        data_prop_qnames = set(model.data_props.keys())
        annex_height = defaultdict(float)
        for cq, exprs in model.subclass_restriction_exprs.items():
            for expr in exprs:
                annex_height[cq] += count_annex_hub_boxes(model.g, expr, data_prop_qnames, model.qname) * self.ANNEX_SLOT_H
        for cq, exprs in model.equivalent_class_exprs.items():
            for expr in exprs:
                annex_height[cq] += count_annex_hub_boxes(model.g, expr, data_prop_qnames, model.qname) * self.ANNEX_SLOT_H
        # one disjointness hub per set, anchored at its first member (see
        # emit_disjoint's `present[0]`) -- every member is a real modeled
        # class in practice, so dset[0] reliably matches emit_disjoint's own
        # anchor choice.
        for dset in model.disjoint_class_sets:
            if dset and dset[0] in model.classes:
                annex_height[dset[0]] += self.ANNEX_SLOT_H
        return annex_height

    def _class_block_height(self, cq, dp_stack_count, annex_height=None):
        h = self.CLASS_H + dp_stack_count.get(cq, 0) * (self.DP_H + self.DP_STACK_GAP)
        if annex_height:
            h += annex_height.get(cq, 0)
        return h

    def _compute_layout_builtin(self):
        model = self.model
        classes = model.classes
        class_list = list(classes.keys())

        if not class_list:
            return 0, 0

        # -- adjacency used for layering and crossing-reduction --
        # subclass edges are directed (child -> parent) and drive layering;
        # object-property edges are undirected for this purpose (they just
        # pull related classes into neighbouring columns).
        children_of = defaultdict(list)   # parent -> [child, ...]
        parents_of = defaultdict(list)    # child -> [parent, ...]
        for cq, c in classes.items():
            for sup in c["subClassOf"]:
                if sup in classes:
                    children_of[sup].append(cq)
                    parents_of[cq].append(sup)

        related = defaultdict(set)        # class -> {class, ...} via object properties
        for p in model.obj_props.values():
            if p["domain"] in classes and p["range"] in classes and p["domain"] != p["range"]:
                related[p["domain"]].add(p["range"])
                related[p["range"]].add(p["domain"])

        # -- 1) layer assignment --
        roots = [cq for cq in class_list if not parents_of.get(cq)]
        layer = {}

        # longest-path layering down the subclass forest (BFS from roots
        # guarantees a class's layer is only finalised after all its
        # ancestors, since parents_of only ever points at already-visited
        # classes in a DAG with no cycles -- OWL subclass hierarchies here
        # are asserted per-class from a Protege export, not user-drawn, so
        # cycles are not a realistic input).
        queue = deque(roots)
        for r in roots:
            layer[r] = 0
        while queue:
            cur = queue.popleft()
            for ch in children_of.get(cur, ()):
                candidate = layer[cur] + 1
                if ch not in layer or candidate > layer[ch]:
                    layer[ch] = candidate
                    queue.append(ch)

        # classes untouched by the subclass forest (no parents AND no
        # children -- true singletons) get layered by BFS distance along
        # object-property edges from any already-layered class, so a class
        # like CustomerService that only relates to UniD via hasOrigin still
        # ends up near UniD instead of dumped in a fixed bucket.
        unlayered = [cq for cq in class_list if cq not in layer]
        changed = True
        while unlayered and changed:
            changed = False
            still_unlayered = []
            for cq in unlayered:
                neighbour_layers = [layer[n] for n in related.get(cq, ()) if n in layer]
                if neighbour_layers:
                    layer[cq] = round(sum(neighbour_layers) / len(neighbour_layers))
                    changed = True
                else:
                    still_unlayered.append(cq)
            unlayered = still_unlayered
        # anything left has no subclass AND no object-property connection at
        # all to the rest of the ontology -- place it in its own top layer
        for cq in unlayered:
            layer[cq] = 0

        max_layer = max(layer.values())
        layers = [[] for _ in range(max_layer + 1)]
        for cq in class_list:
            layers[layer[cq]].append(cq)

        # -- 2) crossing reduction: barycenter heuristic --
        # initial column order within each layer: group siblings together
        # (classes sharing a parent stay adjacent) for a sane starting point
        for lst in layers:
            lst.sort(key=lambda cq: (parents_of.get(cq, [""])[0], cq))
        column = {cq: i for lst in layers for i, cq in enumerate(lst)}

        def reorder_pass(upper_to_lower):
            layer_range = range(1, len(layers)) if upper_to_lower else range(len(layers) - 2, -1, -1)
            for li in layer_range:
                neighbour_layer = li - 1 if upper_to_lower else li + 1
                neighbour_cols = {cq: column[cq] for cq in layers[neighbour_layer]}

                def barycenter(cq):
                    neighbours = (parents_of.get(cq, []) if upper_to_lower else children_of.get(cq, []))
                    neighbours = [n for n in neighbours if n in neighbour_cols]
                    if not neighbours:
                        neighbours = [n for n in related.get(cq, ()) if n in neighbour_cols]
                    if not neighbours:
                        return column[cq]  # keep previous position, no pull
                    return sum(neighbour_cols[n] for n in neighbours) / len(neighbours)

                layers[li].sort(key=lambda cq: (barycenter(cq), column[cq]))
                for i, cq in enumerate(layers[li]):
                    column[cq] = i

        for _ in range(4):
            reorder_pass(upper_to_lower=True)
            reorder_pass(upper_to_lower=False)

        # -- how many datatype-property / restriction boxes, and how much
        # equivalentClass/disjoint annex height, stack under each class, so
        # row heights grow to fit them without colliding with the row below --
        dp_stack_count = self._compute_dp_stack_counts()
        annex_height = self._compute_annex_stack_heights()

        # -- 3) coordinate assignment --
        return self._assign_layers_grid(layers, dp_stack_count, annex_height)

    def _assign_layers_grid(self, layers, dp_stack_count, annex_height=None):
        """Lay out `layers` (a list of rows, top-to-bottom, each a list of
        class qnames in left-to-right order) onto a fixed grid: MAX_ROW_COLS
        columns per row (wider rows wrap into further sub-rows), COL_GAP
        apart, with row height grown to fit each row's tallest stacked
        datatype-property/restriction/annex column. Shared by both layout
        engines so a rank/layer of any size -- however it was ordered --
        always renders as the same readable, overlap-free grid; only how
        `layers` itself gets computed (barycenter heuristic vs. Graphviz
        `dot`) differs between them."""
        def class_block_height(cq):
            return self._class_block_height(cq, dp_stack_count, annex_height)

        margin = 60
        y = margin
        max_x = 0
        cols = self._wrap_cols()

        for lst in layers:
            chunks = [lst[i:i + cols] for i in range(0, len(lst), cols)] or [[]]
            for chunk in chunks:
                row_h = max((class_block_height(cq) for cq in chunk), default=self.CLASS_H)
                for i, cq in enumerate(chunk):
                    x = margin + i * self.COL_GAP
                    self.positions[cq] = (x, y)
                    max_x = max(max_x, x + self.CLASS_W)
                y += row_h + self.SUBROW_GAP
            y += self.LAYER_GAP - self.SUBROW_GAP  # extra breathing room between layers

        return max_x, y

    # -- graphviz `dot` layout ---------------------------------------------
    #
    # Positions classes by shelling out to Graphviz's `dot` instead of the
    # hand-rolled layered layout above. subClassOf edges (parent -> child)
    # drive dot's rank assignment, same top-to-bottom hierarchy semantics as
    # the builtin layout's layer numbers; object-property edges are added
    # with constraint=false so they pull related classes into nearby
    # columns (via dot's crossing-reduction) without perturbing the ranks.
    # Classes get synthetic node ids (n0, n1, ...) so no qname needs
    # dot-identifier quoting/escaping.

    GRAPHVIZ_PX_PER_INCH = 100.0

    def _compute_layout_graphviz(self):
        model = self.model
        classes = model.classes
        class_list = list(classes.keys())

        if not class_list:
            return 0, 0

        if shutil.which("dot") is None:
            sys.exit(
                "--layout graphviz requires the `dot` binary, but it was not "
                "found on PATH. Install Graphviz, e.g.:\n"
                "  brew install graphviz\n"
                "or rerun with --layout builtin (the default)."
            )

        px = self.GRAPHVIZ_PX_PER_INCH
        dp_stack_count = self._compute_dp_stack_counts()
        annex_height = self._compute_annex_stack_heights()

        def class_block_height(cq):
            return self._class_block_height(cq, dp_stack_count, annex_height)

        dot_id = {cq: f"n{i}" for i, cq in enumerate(class_list)}

        lines = [
            "digraph G {",
            "rankdir=TB;",
            f"nodesep={(self.COL_GAP - self.CLASS_W) / px:.3f};",
            f"ranksep={self.LAYER_GAP / px:.3f};",
            "node [shape=box, fixedsize=true, label=\"\"];",
        ]
        if self.waypoint_routing:
            # Ask dot for genuine right-angle, node-avoiding edge paths. Only
            # needed when we're going to keep those paths (waypoint routing);
            # for the default re-grid path we discard dot's geometry anyway,
            # and ortho routing is markedly slower, so we skip it there.
            lines.append("splines=ortho;")
        for cq in class_list:
            w = self.CLASS_W / px
            h = class_block_height(cq) / px
            lines.append(f"{dot_id[cq]} [width={w:.3f}, height={h:.3f}];")
        for cq, c in classes.items():
            for sup in c["subClassOf"]:
                if sup in classes:
                    lines.append(f"{dot_id[sup]} -> {dot_id[cq]};")
        for p in model.obj_props.values():
            if p["domain"] in classes and p["range"] in classes and p["domain"] != p["range"]:
                lines.append(f'{dot_id[p["domain"]]} -> {dot_id[p["range"]]} [constraint=false];')
        # equivalentClass / subClassOf-restriction targets, and disjoint-set
        # siblings, get the same treatment: render_as_class_link/emit_disjoint
        # will draw a real edge or hub-edge between these classes regardless
        # of where dot puts them, so telling dot about the relationship (as a
        # non-rank-constraining edge, same as object properties above) lets
        # its crossing-minimization pull them into nearby columns instead of
        # leaving that edge to cut across the whole diagram by accident.
        seen_soft_edges = set()

        def add_soft_edge(a, b):
            if a in classes and b in classes and a != b and (a, b) not in seen_soft_edges:
                seen_soft_edges.add((a, b))
                lines.append(f"{dot_id[a]} -> {dot_id[b]} [constraint=false];")

        for cq, exprs in model.equivalent_class_exprs.items():
            for expr in exprs:
                for target in expr_referenced_classes(model.g, expr):
                    add_soft_edge(cq, model.qname(target))
        for cq, exprs in model.subclass_restriction_exprs.items():
            for expr in exprs:
                for target in expr_referenced_classes(model.g, expr):
                    add_soft_edge(cq, model.qname(target))
        for dset in model.disjoint_class_sets:
            for a, b in zip(dset, dset[1:]):
                add_soft_edge(a, b)
        lines.append("}")
        dot_source = "\n".join(lines)

        result = subprocess.run(
            ["dot", "-Tplain"], input=dot_source, capture_output=True, text=True,
        )
        if result.returncode != 0:
            sys.exit(f"graphviz `dot` failed:\n{result.stderr}")

        id_to_qname = {v: k for k, v in dot_id.items()}

        if self.waypoint_routing:
            return self._apply_graphviz_geometry(
                result.stdout, id_to_qname, class_list, class_block_height, px)

        raw_centers = {}  # qname -> (x_in, y_in) center, in inches -- used
        # only to recover dot's RANK (y_in) and its crossing-reduced
        # left-to-right ORDER within that rank (x_in); the actual pixel
        # coordinates below come from the same fixed grid the builtin layout
        # uses (see _assign_layers_grid), not from dot's raw inches. dot has
        # no notion of MAX_ROW_COLS, and earlier attempts to keep dot's exact
        # x positions for small ranks while only re-flowing oversized ones
        # left those un-touched ranks positioned relative to the *original*
        # (unwrapped) full-width tree -- e.g. a lone root class centered
        # over the whole tree ended up thousands of pixels to the right of
        # every wrapped row beneath it. Feeding dot's rank+order into the
        # same grid assigner as the builtin layout sidesteps that entirely:
        # every row -- from either engine -- is laid out identically, so
        # `--layout graphviz` alone (straight edges) only changes class
        # ORDER, from dot's global crossing-minimization rather than the
        # builtin layout's local barycenter passes. (Keeping dot's actual
        # coordinates is reserved for --edge-routing graphviz below, where
        # the edge waypoints and node positions have to share one space.)
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "node":
                cq = id_to_qname.get(parts[1])
                if cq is not None:
                    raw_centers[cq] = (float(parts[2]), float(parts[3]))

        rank_members = defaultdict(list)
        for cq in class_list:
            _, y_in = raw_centers[cq]
            rank_members[y_in].append(cq)

        # dot: higher y_in = higher up the page (TB layout, root-first) --
        # sort ranks top-to-bottom, and each rank's members left-to-right by
        # dot's own x_in, preserving its crossing-reduction order.
        layers = [
            sorted(rank_members[y_in], key=lambda cq: raw_centers[cq][0])
            for y_in in sorted(rank_members, reverse=True)
        ]
        return self._assign_layers_grid(layers, dp_stack_count, annex_height)

    def _apply_graphviz_geometry(self, plain, id_to_qname, class_list, class_block_height, px):
        """Waypoint routing: keep dot's actual node coordinates AND its
        computed splines=ortho edge paths, both converted from dot's
        bottom-left-origin inches into drawio's top-left-origin pixels. The
        node positions and edge waypoints MUST come from the same `dot` run
        in the same coordinate space, which is why this path does not re-grid
        (unlike the straight-edge path): re-gridding would move the classes
        out from under the very edge paths dot routed around them.

        `plain` is dot's -Tplain output. Its lines are:
          graph <scale> <width_in> <height_in>
          node  <id> <cx_in> <cy_in> <w_in> <h_in> ...
          edge  <tail> <head> <n> <x1> <y1> ... <xn> <yn> [label ...] ...
        """
        margin = 60
        graph_h_in = 0.0
        node_lines, edge_lines = [], []
        for line in plain.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "graph":
                graph_h_in = float(parts[3])
            elif parts[0] == "node":
                node_lines.append(parts)
            elif parts[0] == "edge":
                edge_lines.append(parts)

        def to_px(x_in, y_in):
            # flip Y (dot: origin bottom-left, y up; drawio: top-left, y down)
            return (x_in * px + margin, (graph_h_in - y_in) * px + margin)

        max_x = max_y = 0.0
        for parts in node_lines:
            cq = id_to_qname.get(parts[1])
            if cq is None:
                continue
            cx_in, cy_in = float(parts[2]), float(parts[3])
            cx_px, cy_px = to_px(cx_in, cy_in)
            # dot centres each node on the FULL block height it was given
            # (class box + datatype-property/annex stacks); place the class
            # box at the block's top so those stacks fill the reserved space
            # downward, exactly as emit_data_properties/add_near expect.
            h = class_block_height(cq)
            x = cx_px - self.CLASS_W / 2
            y = cy_px - h / 2
            self.positions[cq] = (x, y)
            max_x = max(max_x, x + self.CLASS_W)
            max_y = max(max_y, y + h)

        for parts in edge_lines:
            tail_q = id_to_qname.get(parts[1])
            head_q = id_to_qname.get(parts[2])
            if tail_q is None or head_q is None:
                continue
            n = int(parts[3])
            coords = parts[4:4 + 2 * n]
            pts = [to_px(float(coords[i]), float(coords[i + 1])) for i in range(0, 2 * n, 2)]
            # dot's ortho output repeats control points at each bend; collapse
            # runs of (near-)identical points so the drawio polyline is clean.
            deduped = []
            for p in pts:
                if not deduped or abs(p[0] - deduped[-1][0]) > 0.5 or abs(p[1] - deduped[-1][1]) > 0.5:
                    deduped.append(p)
            self.edge_waypoints[(tail_q, head_q)].append(deduped)

        return max_x, max_y

    # -- ELK (Eclipse Layout Kernel) layout --------------------------------
    #
    # Shells out to Node + elkjs (via elk_layout.js) for a layered layout
    # with ORTHOGONAL edge routing. ELK's decisive advantage over both the
    # builtin and graphviz layouts for dense ontologies is that it treats
    # each edge LABEL as a first-class box with a real size and reserves
    # layout space for it -- so relationship names stop piling on top of one
    # another, which was the remaining readability problem. Like the graphviz
    # waypoint path, ELK's node positions AND its routed edge paths are kept
    # verbatim (they share one coordinate space and must not be re-gridded);
    # ELK already uses drawio's top-left / y-down convention, so no Y flip is
    # needed. The graph is generic -- classes and their subClassOf / object-
    # property / equivalentClass / disjoint relationships -- with no
    # ontology-specific knowledge.

    ELK_HELPER = "elk_layout.js"

    @staticmethod
    def _label_box_size(text):
        """Rough px size for an edge label, so ELK can reserve space for it.
        A generous per-character width keeps ELK from under-reserving and
        letting labels overlap; exact metrics don't matter, only that longer
        names claim proportionally more room."""
        return (max(30, int(len(text) * 7.5) + 12), 20)

    def _run_elk(self, helper, graph):
        result = subprocess.run(
            ["node", helper], input=json.dumps(graph), capture_output=True, text=True,
        )
        if result.returncode != 0:
            sys.exit(f"ELK layout failed:\n{result.stderr}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            sys.exit(f"ELK returned invalid JSON: {e}\n{result.stdout[:500]}")

    @staticmethod
    def _elk_port_side_from_point(rect, point):
        """Classify which side of `rect` (x, y, w, h) a point sits nearest
        -- used to learn, from an unpadded first ELK pass, which side of a
        class box an edge naturally wants to attach to before the real
        (padded) pass forces that decision to be made explicit via a
        FIXED_POS port."""
        x, y, w, h = rect
        px, py = point
        d_n, d_s = abs(py - y), abs(py - (y + h))
        d_w, d_e = abs(px - x), abs(px - (x + w))
        m = min(d_n, d_s, d_w, d_e)
        if m == d_n:
            return "N"
        if m == d_s:
            return "S"
        if m == d_w:
            return "W"
        return "E"

    def _build_elk_edge_specs(self):
        """(source_qname, target_qname, label_or_None) for every edge the
        ELK layout positions on: subClassOf (parent -> child, unlabeled),
        object properties with both domain and range resolved to modeled,
        distinct classes (labeled with the property qname), and unlabeled
        'soft' clustering edges for equivalentClass targets, subclass-
        restriction targets, and disjoint siblings -- same edge set
        _compute_layout_elk has always built, now shared with the new
        unpadded first pass in this same method."""
        model = self.model
        classes = model.classes
        specs = []
        for cq, c in classes.items():
            for sup in c["subClassOf"]:
                if sup in classes:
                    specs.append((sup, cq, None))
        for pq, p in model.obj_props.items():
            d, r = p["domain"], p["range"]
            if d in classes and r in classes and d != r:
                specs.append((d, r, pq))
        seen_soft = set()

        def add_soft(a, b):
            if a in classes and b in classes and a != b and (a, b) not in seen_soft:
                seen_soft.add((a, b))
                specs.append((a, b, None))

        for cq, exprs in model.equivalent_class_exprs.items():
            for expr in exprs:
                for t in expr_referenced_classes(model.g, expr):
                    add_soft(cq, model.qname(t))
        for cq, exprs in model.subclass_restriction_exprs.items():
            for expr in exprs:
                for t in expr_referenced_classes(model.g, expr):
                    add_soft(cq, model.qname(t))
        for dset in model.disjoint_class_sets:
            for a, b in zip(dset, dset[1:]):
                add_soft(a, b)
        return specs

    def _compute_layout_elk(self):
        if self.elk_mode == "stress":
            return self._compute_layout_elk_stress()

        model = self.model
        classes = model.classes
        class_list = list(classes.keys())
        if not class_list:
            return 0, 0

        if shutil.which("node") is None:
            sys.exit(
                "--layout elk requires Node.js, but the `node` binary was not "
                "found on PATH. Install Node.js, or rerun with --layout "
                "graphviz / --layout builtin."
            )
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.ELK_HELPER)
        if not os.path.exists(helper):
            sys.exit(f"--layout elk requires {helper}, which is missing.")
        if not os.path.isdir(os.path.join(os.path.dirname(helper), "node_modules", "elkjs")):
            sys.exit(
                "--layout elk requires the elkjs npm package, which is not "
                "installed. Install it once with:\n"
                f"  cd {os.path.dirname(helper)} && npm install\n"
                "then rerun."
            )

        dp_stack_count = self._compute_dp_stack_counts()
        annex_height = self._compute_annex_stack_heights()

        def class_block_height(cq):
            return self._class_block_height(cq, dp_stack_count, annex_height)

        def has_stack(cq):
            return class_block_height(cq) > self.CLASS_H + 0.5

        node_id = {cq: f"n{i}" for i, cq in enumerate(class_list)}
        id_to_qname = {v: k for k, v in node_id.items()}
        edge_specs = self._build_elk_edge_specs()

        base_options = {
            "elk.algorithm": "layered",
            "elk.direction": "DOWN",
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.layered.spacing.nodeNodeBetweenLayers": str(self.LAYER_GAP),
            "elk.spacing.nodeNode": "60",
            "elk.spacing.edgeNode": "30",
            "elk.spacing.edgeEdge": "25",
            "elk.layered.spacing.edgeEdgeBetweenLayers": "20",
            "elk.layered.spacing.edgeNodeBetweenLayers": "30",
            "elk.spacing.edgeLabel": "8",
            "elk.edgeLabels.placement": "CENTER",
            "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
        }

        # -- pass 1: TRUE unpadded sizes, no ports -- purely to learn which
        # side of each class box every edge naturally wants to attach to,
        # before pass 2's padded node heights would force that choice into
        # the dead space below a class's own DP/annex stack (see this
        # method's docstring above for why that's a real defect).
        p1_children = [
            {"id": node_id[cq], "width": self.CLASS_W, "height": self.CLASS_H}
            for cq in class_list
        ]
        p1_edges = [
            {"id": f"p1e{i}", "sources": [node_id[a]], "targets": [node_id[b]]}
            for i, (a, b, _label) in enumerate(edge_specs)
        ]
        p1_laid = self._run_elk(helper, {
            "id": "root", "layoutOptions": base_options,
            "children": p1_children, "edges": p1_edges,
        })
        p1_rect = {}
        for child in p1_laid.get("children", []):
            cq = id_to_qname.get(child["id"])
            if cq:
                p1_rect[cq] = (child.get("x", 0.0), child.get("y", 0.0), self.CLASS_W, self.CLASS_H)
        natural_side = {}
        for i, e in enumerate(p1_laid.get("edges", [])):
            secs = e.get("sections", [])
            if not secs:
                continue
            a, b, _label = edge_specs[i]
            sp, ep = secs[0].get("startPoint"), secs[0].get("endPoint")
            if a in p1_rect and sp:
                natural_side[(i, "source")] = self._elk_port_side_from_point(
                    p1_rect[a], (sp["x"], sp["y"]))
            if b in p1_rect and ep:
                natural_side[(i, "target")] = self._elk_port_side_from_point(
                    p1_rect[b], (ep["x"], ep["y"]))

        # -- pass 2: real (padded) sizes. Any class with a DP/annex stack
        # gets explicit FIXED_POS ports for every edge touching it: pass-1's
        # natural side is kept for N/E/W, but a natural S is redirected to W
        # (spaced within the class's real 40px header) since S is where the
        # dead stack space now is.
        port_cursor = defaultdict(int)
        node_ports = defaultdict(list)
        edge_port_ref = {}
        needs_ports = {cq for cq in class_list if has_stack(cq)}

        def assign_port(cq, side, i, which):
            pid = f"p_{node_id[cq]}_{i}_{which}"
            w, h = self.CLASS_W, class_block_height(cq)
            if side == "S":
                slot = port_cursor[cq]
                port_cursor[cq] += 1
                y = 8 + (slot % 3) * 10
                node_ports[cq].append({"id": pid, "x": 0, "y": y, "width": 1, "height": 1})
            elif side == "N":
                node_ports[cq].append({"id": pid, "x": w / 2, "y": 0, "width": 1, "height": 1})
            elif side == "W":
                node_ports[cq].append({"id": pid, "x": 0, "y": self.CLASS_H / 2, "width": 1, "height": 1})
            else:
                node_ports[cq].append({"id": pid, "x": w, "y": self.CLASS_H / 2, "width": 1, "height": 1})
            edge_port_ref[(i, which)] = pid
            id_to_qname[pid] = cq

        for i, (a, b, _label) in enumerate(edge_specs):
            for which, cq in (("source", a), ("target", b)):
                if cq in needs_ports:
                    assign_port(cq, natural_side.get((i, which), "N"), i, which)

        p2_children = []
        for cq in class_list:
            child = {"id": node_id[cq], "width": self.CLASS_W, "height": class_block_height(cq)}
            if cq in needs_ports:
                child["ports"] = node_ports[cq]
                child["layoutOptions"] = {"elk.portConstraints": "FIXED_POS"}
            p2_children.append(child)

        p2_edges = []
        for i, (a, b, label) in enumerate(edge_specs):
            e = {
                "id": f"e{i}",
                "sources": [edge_port_ref.get((i, "source"), node_id[a])],
                "targets": [edge_port_ref.get((i, "target"), node_id[b])],
            }
            if label:
                lw, lh = self._label_box_size(label)
                e["labels"] = [{"id": f"e{i}l", "text": label, "width": lw, "height": lh}]
            p2_edges.append(e)

        laid = self._run_elk(helper, {
            "id": "root", "layoutOptions": base_options,
            "children": p2_children, "edges": p2_edges,
        })

        margin = 60
        max_x = max_y = 0.0
        for child in laid.get("children", []):
            cq = id_to_qname.get(child["id"])
            if cq is None:
                continue
            x = child.get("x", 0.0) + margin
            y = child.get("y", 0.0) + margin
            self.positions[cq] = (x, y)
            max_x = max(max_x, x + self.CLASS_W)
            max_y = max(max_y, y + class_block_height(cq))

        for e in laid.get("edges", []):
            tail_q = id_to_qname.get(e["sources"][0]) if e.get("sources") else None
            head_q = id_to_qname.get(e["targets"][0]) if e.get("targets") else None
            if tail_q is None or head_q is None:
                continue
            for sec in e.get("sections", []):
                pts = [(sec["startPoint"]["x"] + margin, sec["startPoint"]["y"] + margin)]
                for bp in sec.get("bendPoints", []):
                    pts.append((bp["x"] + margin, bp["y"] + margin))
                pts.append((sec["endPoint"]["x"] + margin, sec["endPoint"]["y"] + margin))
                deduped = []
                for pt in pts:
                    if not deduped or abs(pt[0] - deduped[-1][0]) > 0.5 or abs(pt[1] - deduped[-1][1]) > 0.5:
                        deduped.append(pt)
                self.edge_waypoints[(tail_q, head_q)].append(deduped)

        return max_x, max_y

    def _compute_layout_elk_stress(self):
        """Force-directed ELK layout (org.eclipse.elk.stress) for dense,
        association-heavy, shallow-hierarchy ontologies that sprawl into a
        wide 2:1 ribbon under the layered algorithm. Two ELK passes: stress
        places node centers by spring embedding (it does not itself keep
        rectangles from overlapping), then sporeOverlap de-overlaps them
        from those same coordinates with minimal displacement. Unlike
        _compute_layout_elk, there are no layers/sides/ports to reason
        about here, and no disjoint-set clustering edges (a clique of N
        mutually-disjoint classes held at a fixed edge length inflates into
        a large ring and flings its members to the diagram's periphery)."""
        model = self.model
        classes = model.classes
        class_list = list(classes.keys())
        if not class_list:
            return 0, 0

        if shutil.which("node") is None:
            sys.exit(
                "--layout elk requires Node.js, but the `node` binary was not "
                "found on PATH. Install Node.js, or rerun with --layout "
                "graphviz / --layout builtin."
            )
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.ELK_HELPER)
        if not os.path.exists(helper):
            sys.exit(f"--layout elk requires {helper}, which is missing.")
        if not os.path.isdir(os.path.join(os.path.dirname(helper), "node_modules", "elkjs")):
            sys.exit(
                "--layout elk requires the elkjs npm package, which is not "
                "installed. Install it once with:\n"
                f"  cd {os.path.dirname(helper)} && npm install\n"
                "then rerun."
            )

        dp_stack_count = self._compute_dp_stack_counts()
        annex_height = self._compute_annex_stack_heights()

        def class_block_height(cq):
            return self._class_block_height(cq, dp_stack_count, annex_height)

        node_id = {cq: f"n{i}" for i, cq in enumerate(class_list)}
        id_to_qname = {v: k for k, v in node_id.items()}
        degrees = self._compute_degrees()
        scale = min(self.STRESS_SCALE_MAX, max(
            self.STRESS_SCALE_MIN, math.sqrt(len(class_list) / self.STRESS_SCALE_REF)
        ))
        base_len = self.STRESS_EDGE_LEN_BASE * scale

        def edge_len(a, b, is_subclass):
            if is_subclass:
                return base_len  # subclass edges stay short: keeps the taxonomy tight
            deg = max(degrees.get(a, 0), degrees.get(b, 0))
            factor = min(self.STRESS_DEGREE_LEN_CAP, max(
                1.0, 1 + self.STRESS_DEGREE_FACTOR * (deg - self.STRESS_DEGREE_REF)
            ))
            return base_len * factor

        children = [
            {"id": node_id[cq], "width": self.CLASS_W, "height": class_block_height(cq)}
            for cq in class_list
        ]
        edges = []
        eid = [0]

        def add_stress_edge(a, b, is_subclass, label=None):
            eid[0] += 1
            e = {
                "id": f"e{eid[0]}", "sources": [node_id[a]], "targets": [node_id[b]],
                "layoutOptions": {"elk.stress.desiredEdgeLength": str(round(edge_len(a, b, is_subclass)))},
            }
            if label:
                lw, lh = self._label_box_size(label)
                e["labels"] = [{"id": f"e{eid[0]}l", "text": label, "width": lw, "height": lh}]
            edges.append(e)

        for cq, c in classes.items():
            for sup in c["subClassOf"]:
                if sup in classes:
                    add_stress_edge(sup, cq, is_subclass=True)
        for pq, p in model.obj_props.items():
            d, r = p["domain"], p["range"]
            if d in classes and r in classes and d != r:
                add_stress_edge(d, r, is_subclass=False, label=pq)
        # No disjoint/equivalentClass soft clustering edges under stress --
        # see this method's docstring.

        laid = self._run_elk(helper, {
            "id": "root",
            "layoutOptions": {
                "elk.algorithm": "stress",
                "elk.stress.desiredEdgeLength": str(round(base_len)),
            },
            "children": children, "edges": edges,
        })

        overlap_children = []
        for child in laid.get("children", []):
            cq = id_to_qname.get(child["id"])
            if cq is None:
                continue
            overlap_children.append({
                "id": child["id"], "x": child.get("x", 0.0), "y": child.get("y", 0.0),
                "width": self.CLASS_W, "height": class_block_height(cq),
            })
        deoverlapped = self._run_elk(helper, {
            "id": "root",
            "layoutOptions": {"elk.algorithm": "sporeOverlap", "elk.spacing.nodeNode": "60"},
            "children": overlap_children, "edges": [],
        })

        margin = 60
        max_x = max_y = 0.0
        for child in deoverlapped.get("children", []):
            cq = id_to_qname.get(child["id"])
            if cq is None:
                continue
            x = child.get("x", 0.0) + margin
            y = child.get("y", 0.0) + margin
            self.positions[cq] = (x, y)
            max_x = max(max_x, x + self.CLASS_W)
            max_y = max(max_y, y + class_block_height(cq))
        return max_x, max_y

    # -- emission passes ----------------------------------------------------

    def emit_classes(self):
        model = self.model
        for cq, c in model.classes.items():
            if cq not in self.positions:
                continue
            x, y = self.positions[cq]
            ns = cq.split(":")[0]
            defining_ns = self._defining_namespace()
            style = self.CLASS_STYLE if ns == defining_ns else self.EXTERNAL_CLASS_STYLE
            if ns == defining_ns:
                color = self._category_colors.get(self._categories.get(cq))
                if color:
                    style += f"fillColor={color[0]};strokeColor={color[1]};"
            if self._degrees.get(cq, 0) >= self.HUB_HIGHLIGHT_DEGREE:
                style += "strokeWidth=3;"
            cid = self.add_vertex_hub(x, y, self.CLASS_W, self.CLASS_H, cq, style)
            self.class_ids[cq] = cid
            self._class_qname_by_id[cid] = cq

    def _defining_namespace(self):
        """Best guess at the ontology's own prefix: whichever prefix is used
        by the largest number of classes."""
        counts = defaultdict(int)
        for cq in self.model.classes:
            counts[cq.split(":")[0]] += 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def _compute_categories(self):
        """category(cq) = the topmost subClassOf ancestor reachable from cq
        (a class with no modeled superclass is its own category). Computed
        as an iterative fixpoint (not recursive DFS) so it's correct AND
        order-independent even when subClassOf contains a cycle (invalid
        OWL, but not something this generator should crash or waffle on):
        each node's root-set only ever grows via union, so the fixpoint
        this converges to does not depend on which node is processed first,
        in any pass. Classes with multiple root ancestors (diamond
        inheritance) are pinned to the alphabetically-first one for the
        final category label. Only categories with >= MIN_CATEGORY_SIZE
        members get a color; everything else stays white."""
        classes = self.model.classes
        roots = {}
        for cq in classes:
            sups = [s for s in classes[cq]["subClassOf"] if s in classes]
            roots[cq] = frozenset([cq]) if not sups else frozenset()

        changed = True
        while changed:
            changed = False
            for cq in classes:
                sups = [s for s in classes[cq]["subClassOf"] if s in classes]
                if not sups:
                    continue
                new_roots = frozenset().union(*(roots[s] for s in sups))
                if new_roots != roots[cq]:
                    roots[cq] = new_roots
                    changed = True

        categories = {cq: (min(roots[cq]) if roots[cq] else cq) for cq in classes}

        counts = defaultdict(int)
        for cat in categories.values():
            counts[cat] += 1
        eligible = sorted(
            (cat for cat, n in counts.items() if n >= self.MIN_CATEGORY_SIZE),
            key=lambda cat: (-counts[cat], cat),
        )
        colors = {
            cat: self.CATEGORY_PALETTE[i % len(self.CATEGORY_PALETTE)]
            for i, cat in enumerate(eligible)
        }
        return categories, colors

    def _compute_degrees(self):
        """Incident-edge count per class: every subClassOf edge (both ends)
        plus every object-property edge with both domain and range resolved
        to a modeled class (both ends, or once for a domain==range
        self-loop). Used for hub highlighting and, under --elk-mode stress,
        to spread edges converging on a hub (see _compute_layout_elk_stress)."""
        classes = self.model.classes
        degrees = defaultdict(int)
        for cq, c in classes.items():
            for sup in c["subClassOf"]:
                if sup in classes:
                    degrees[cq] += 1
                    degrees[sup] += 1
        for p in self.model.obj_props.values():
            d, r = p["domain"], p["range"]
            if d in classes and r in classes:
                degrees[d] += 1
                if r != d:
                    degrees[r] += 1
        return degrees

    def emit_subclass_edges(self):
        for cq, c in self.model.classes.items():
            for sup in c["subClassOf"]:
                if cq in self.class_ids and sup in self.class_ids:
                    # Child exits north (always safe -- nothing but the
                    # parent sits above it); parent is entered from the
                    # south, redirected to west by _side_point (plus a
                    # clear-column detour) if the parent itself has a
                    # datatype-property stack.
                    ex, ey = self._side_point(cq, "N", 0.5)
                    nx, ny = self._side_point(sup, "S", 0.5)
                    style = (
                        self.SUBCLASS_EDGE_STYLE
                        + f"exitX={ex};exitY={ey};exitDx=0;exitDy=0;"
                        + f"entryX={nx};entryY={ny};entryDx=0;entryDy=0;"
                    )
                    extra_points = list(reversed(
                        self._stack_clearance_points(sup, "S", 0.5, cq, "N")
                    ))
                    self.add_edge(self.class_ids[cq], self.class_ids[sup], style,
                                  extra_points=extra_points)

    # Fractional positions (along a box's side, matching CLASS_STYLE's own
    # snap points) handed out round-robin per class. Object-property edges
    # left with NO exit/entry constraint all float to whichever perimeter
    # point is nearest the other endpoint -- fine for one edge, but a class
    # with a dozen object properties ends up with a dozen arrows converging
    # on nearly the same point and passing straight through its neighbours.
    # Spreading them across these slots fans the arrows out along the side
    # instead, which is also just what a person drawing this by hand would do.
    ANCHOR_FRACS = [0.5, 0.25, 0.75, 0.15, 0.85, 0.35, 0.65, 0.1, 0.9]

    def _next_anchor_frac(self, cq):
        slot = self._anchor_slot[cq]
        self._anchor_slot[cq] += 1
        return self.ANCHOR_FRACS[slot % len(self.ANCHOR_FRACS)]

    def _has_dp_stack(self, cq):
        return self._stack_heights.get(cq, self.CLASS_H) > self.CLASS_H + 0.5

    def _side_point(self, cq, side, frac):
        """(x-fraction, y-fraction) for an exit/entry constraint on `cq`'s
        `side` ('N'/'S'/'E'/'W'). A requested 'S' is redirected to 'W' when
        `cq` has a datatype-property/annex stack: that stack sits flush
        against the class's own south edge (see DP_STACK_GAP), and drawio
        derives an edge's actual connection point from the class's real
        40px geometry toward wherever the line is headed -- so an edge
        anchored to a stacked class's south side is drawn running straight
        through its own stacked boxes to reach that point. N/E/W never
        touch that stack, so they're always safe. Same reasoning as
        _compute_layout_elk's two-pass port pinning, applied here to the
        drawio-side exit/entry constraints that builtin, graphviz (without
        baked waypoints), and --elk-mode stress all use directly (elk
        layered bypasses this entirely via its own FIXED_POS ports)."""
        if side == "S" and self._has_dp_stack(cq):
            side = "W"
        return {
            "N": (frac, 0), "S": (frac, 1), "W": (0, frac), "E": (1, frac),
        }[side]

    # How far a redirected west-side connection steps clear of the class's
    # own column before its vertical run. The exit/entry CONSTRAINT alone
    # (_side_point) guarantees the line's endpoint sits above the stack --
    # but orthogonalEdgeStyle's route to that point is computed by drawio
    # itself with no awareness of other shapes. A SINGLE clearance point at
    # the entry height isn't enough either: drawio still has to decide how
    # to get from the other endpoint to that point, and if the other
    # endpoint sits below/within this class's own column, the vertical leg
    # of that approach can cut straight through the stack on the way
    # (reported: an edge from a lower class into a redirected west entry
    # still crossed the target's own last datatype-property box). Two
    # points -- one at the OTHER endpoint's height, one at this endpoint's
    # entry height, both at the same clear x -- forces the entire vertical
    # run to happen in the clear column instead, so the segment actually
    # arriving at (or leaving) the stacked class is always a flat
    # horizontal line at the safe header height.
    WEST_ENTRY_CLEARANCE = 60
    # Small stand-off so the detour's "far" waypoint sits clearly above or
    # below the OTHER endpoint's own box, never exactly on its boundary.
    # Without this, when the other endpoint exits/enters at fraction 0 or 1
    # (its own top/bottom edge -- the common case), the far point's height
    # coincides exactly with that edge, and the horizontal segment leading
    # to it visibly traces along the box's own border for however much of
    # it lies within that class's width (reported: a line "running along
    # the class box lines" leaving a source that exits north).
    FAR_POINT_MARGIN = 15

    def _stack_clearance_points(self, cq, side, frac, other_cq, other_side):
        """Two absolute waypoints, ordered near-to-far relative to `cq`
        (reverse them when `cq` is the target, so they read far-to-near
        along the actual source->target path), or [] if `side` wasn't
        actually a stack-triggered redirect. `other_side` ('N' or 'S' --
        this is only ever called from the sy<=ty/sy>ty branches, never the
        same-row one) says which way to step the far point off of
        `other_cq`'s own box."""
        if side != "S" or not self._has_dp_stack(cq):
            return []
        x, y = self.positions[cq]
        clear_x = x - self.WEST_ENTRY_CLEARANCE
        this_y = y + frac * self.CLASS_H
        ox, oy = self.positions[other_cq]
        other_y = (oy - self.FAR_POINT_MARGIN if other_side == "N"
                   else oy + self.CLASS_H + self.FAR_POINT_MARGIN)
        return [(clear_x, this_y), (clear_x, other_y)]

    def _anchor_style(self, source_cq, target_cq):
        """(style, extra_points) for an edge between two positioned
        classes: enter/exit the side that actually faces the other class
        (top/bottom if they're in different rows, left/right if they share a
        row), at the next round-robin fraction along that side for each
        class -- redirected off a stacked class's south side via
        _side_point, with a clear-column detour (_stack_clearance_points)
        added for whichever endpoint that redirect applies to. Chowlk's
        parser only reads source/target cell IDs and the style's
        arrowhead/dash keywords for object-property edges -- it does not
        care which point on the perimeter the line touches -- so this is
        purely a drawio rendering concern, safe to vary freely."""
        sx, sy = self.positions[source_cq]
        tx, ty = self.positions[target_cq]
        s_frac = self._next_anchor_frac(source_cq)
        t_frac = self._next_anchor_frac(target_cq)
        if sy == ty:
            s_side, t_side = ("E", "W") if sx <= tx else ("W", "E")
        elif sy <= ty:
            s_side, t_side = "S", "N"
        else:
            s_side, t_side = "N", "S"
        ex, ey = self._side_point(source_cq, s_side, s_frac)
        nx, ny = self._side_point(target_cq, t_side, t_frac)
        style = (
            f"exitX={ex};exitY={ey};exitDx=0;exitDy=0;"
            f"entryX={nx};entryY={ny};entryDx=0;entryDy=0;"
        )
        # Near-to-far relative to source belongs first in the path (source
        # end); target's near-to-far is reversed, since the path arrives AT
        # the target -- its "near" (entry-height) point must be last.
        points = list(self._stack_clearance_points(source_cq, s_side, s_frac, target_cq, t_side))
        points.extend(reversed(
            self._stack_clearance_points(target_cq, t_side, t_frac, source_cq, s_side)
        ))
        return style, points

    def emit_object_properties(self):
        obj_props = self.model.obj_props
        # infer missing domain/range via inverseOf (swap) generically
        inferred = {}
        for pq, p in obj_props.items():
            if p["domain"] and p["range"]:
                continue
            inv = p.get("inverseOf")
            if inv and inv in obj_props:
                invp = obj_props[inv]
                d = p["domain"] or invp["range"]
                r = p["range"] or invp["domain"]
                if d and r:
                    inferred[pq] = (d, r)

        for pq, p in obj_props.items():
            has_domain, has_range = bool(p["domain"]), bool(p["range"])
            d, r = p["domain"], p["range"]
            if pq in inferred:
                d = d or inferred[pq][0]
                r = r or inferred[pq][1]
            if not d or not r or d not in self.class_ids or r not in self.class_ids:
                continue
            anchor_style, extra_points = self._anchor_style(d, r)
            style = self.OP_EDGE_STYLE[(has_domain, has_range)] + anchor_style
            label_style = self.OP_LABEL_STYLE_SOLID if (has_domain and has_range) else self.OP_LABEL_STYLE_DASHED
            label = pq
            if p.get("transitive"):
                label = f"(T) {label}"
            self.add_edge(self.class_ids[d], self.class_ids[r], style, label=label,
                          label_style=label_style, extra_points=extra_points)

    def emit_data_properties(self):
        # Chowlk associates a datatype-property box with its class purely by
        # geometry: the box's top-left corner must sit within 5px of the
        # class box's bottom-left corner (see Chowlk's
        # classify_boxes_into_classes_and_datatype_properties). There is no
        # connecting edge in the notation -- drawing one is actively harmful,
        # since an unlabeled edge with endArrow=none isn't a type Chowlk
        # recognizes and gets reported as a parse error.
        for pq, p in self.model.data_props.items():
            for dom in p["domains"]:
                if dom not in self.class_ids:
                    continue
                idx = self.dp_stack_counts[dom]
                x, y = self.positions[dom]
                dpx, dpy = x, y + self.CLASS_H + idx * (self.DP_H + self.DP_STACK_GAP)
                # Chowlk: "prop: datatype" + solid box when a range is asserted;
                # bare "prop" + dashed box when no range is given.
                if p["range"]:
                    label = f"{pq}: {p['range'].split(':')[-1]}"
                    dp_style = self.DP_STYLE_TYPED
                else:
                    label = pq
                    dp_style = self.DP_STYLE_UNTYPED
                self.add_vertex(dpx, dpy, self.DP_W, self.DP_H, label, dp_style)
                self._dp_box_rects[dom].append((dpx, dpy, self.DP_W, self.DP_H))
                self.dp_stack_counts[dom] += 1

    def emit_equivalent_and_restrictions(self):
        renderer = ExpressionRenderer(self.model, self)
        for cq, exprs in self.model.equivalent_class_exprs.items():
            for expr in exprs:
                renderer.render_as_class_link(cq, expr, "eq")
        for cq, exprs in self.model.subclass_restriction_exprs.items():
            for expr in exprs:
                renderer.render_as_class_link(cq, expr, "")

    def emit_disjoint(self):
        for dset in self.model.disjoint_class_sets:
            present = [d for d in dset if d in self.class_ids]
            if len(present) < 2:
                continue
            # Anchor the hub in the first member's annex lane instead of an
            # inline grid coordinate -- an inline y-position computed from
            # sibling classes can drift into a WHOLLY UNRELATED class's row
            # when disjoint members span multiple grid rows (verified: this
            # collided with an unrelated class in the same connected
            # component before the fix).
            anchor = present[0]
            hub_id = self.add_near(anchor, 60, "⊥", self.DISJOINT_HUB_STYLE, w=30, h=30, is_hub=True)
            for d_ in present:
                self.add_edge(hub_id, self.class_ids[d_], self.HUB_EDGE)

    def emit_individuals(self):
        """Draw named individuals in a grid band beneath the class diagram,
        each linked to its class(es) by an rdf:type arrow and to other
        individuals by its object-property assertions.

        The band sits below everything already emitted rather than being fed
        through a layout engine: the engines are keyed on classes throughout
        (node ids, positions, port pinning), and an ABox is a separate
        concern from the class graph's shape -- keeping it in its own region
        means individuals can never perturb a class layout that was tuned
        and verified without them."""
        individuals = self.model.individuals
        if not individuals:
            return

        # Start below the lowest thing emitted so far. Derived from the
        # rectangles actually placed (not re-computed from class positions)
        # so datatype-property stacks and annex boxes are accounted for.
        if self._vertex_rects:
            base_y = max(y + h for _, _, y, _, h, _ in self._vertex_rects) + self.INDIV_BAND_GAP
            base_x = min(x for _, x, _, _, _, _ in self._vertex_rects)
        else:
            base_x, base_y = 0.0, 0.0

        for idx, iq in enumerate(sorted(individuals)):
            col, row = idx % self.INDIV_MAX_COLS, idx // self.INDIV_MAX_COLS
            x = base_x + col * self.INDIV_COL_GAP
            y = base_y + row * self.INDIV_ROW_GAP
            # The <u> wrapper IS the notation -- see INDIVIDUAL_STYLE.
            cid = self.add_vertex(x, y, self.INDIV_W, self.INDIV_H,
                                  f"<u>{iq}</u>", self.INDIVIDUAL_STYLE)
            self.individual_ids[iq] = cid

        for iq in sorted(individuals):
            for tq in individuals[iq]["types"]:
                if tq in self.class_ids:
                    self.add_edge(
                        self.individual_ids[iq], self.class_ids[tq],
                        self.RDF_TYPE_EDGE_STYLE,
                        label=self.RDF_TYPE_LABEL,
                        label_style=self.RDF_TYPE_LABEL_STYLE,
                    )

        for sq, pq, oq in self.model.individual_relations:
            if sq in self.individual_ids and oq in self.individual_ids:
                self.add_edge(
                    self.individual_ids[sq], self.individual_ids[oq],
                    self.INDIVIDUAL_RELATION_EDGE_STYLE,
                    label=pq, label_style=self.OP_LABEL_STYLE_SOLID,
                )

    def emit_notes(self, title, model_source_path, used_prefixes):
        g = self.model.g
        onto_uri = None
        for s in g.subjects(RDF.type, OWL.Ontology):
            onto_uri = s
            break

        def val(pred_list):
            for pred in pred_list:
                v = g.value(onto_uri, pred) if onto_uri else None
                if v:
                    return str(v)
            return None

        onto_title = title or val([DCTERMS.title, DC.title, RDFS.label]) or model_source_path
        onto_desc = val([DCTERMS.description, DC.description])
        version = val([OWL.versionInfo])

        # Chowlk parses every shape=document box as ontology metadata, keyed
        # by "prefix:suffix: value" lines (or "owl:Ontology: <uri>" for the
        # ontology URI itself). Only emit lines in that exact grammar --
        # free-form prose here would be mis-parsed as bogus metadata triples.
        meta_lines = []
        if onto_uri:
            meta_lines.append(f'<div><b>owl:Ontology</b>: &lt;{self.esc(str(onto_uri))}&gt;</div>')
        meta_lines.append('<div><br/></div>')
        meta_lines.append(f'<div><b>dcterms:title</b>: "{self.esc(onto_title)}"</div>')
        if onto_desc:
            meta_lines.append(f'<div><b>dcterms:description</b>: "{self.esc(onto_desc[:300])}"</div>')
        if version:
            meta_lines.append(f'<div><b>owl:versionInfo</b>: "{self.esc(version)}"</div>')
        meta_value = "".join(meta_lines)
        self.add_vertex(
            40, -320, 420, 200, meta_value,
            "shape=document;whiteSpace=wrap;html=1;boundedLbl=1;labelBackgroundColor=#ffffff;"
            "strokeColor=#000000;fontSize=12;fontColor=#000000;size=0.1875;align=left;verticalAlign=top;spacing=8;",
        )

        # Chowlk parses every shape=note box as "prefix: <namespace-uri>"
        # namespace declarations -- one per line, no other content is safe
        # to put in a note box anywhere on the canvas.
        ns_lines = [
            f'<div><b>{self.esc(prefix)}:</b> {self.esc(ns)}</div>'
            for prefix, ns in sorted(used_prefixes.items())
        ]
        add_x = 500
        self.add_vertex(
            add_x, -320, 340, min(260, 30 + 22 * max(1, len(ns_lines))), "".join(ns_lines) or "(no namespaces)",
            "shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;darkOpacity=0.05;align=left;verticalAlign=top;spacing=8;fontSize=11;",
        )
        # A human-readable legend is useful, but shape=note/shape=document
        # both get parsed as real ontology data by Chowlk regardless of
        # position -- so the legend must use a plain text/rectangle shape
        # that is not one of those two triggers.
        legend_value = (
            '<div><b>Legend (Chowlk notation)</b></div>'
            '<div>box color = class category -- see color key at right for what each color is</div>'
            '<div>blue box = class from another (external) namespace</div>'
            '<div>yellow dashed box = owl:DatatypeProperty</div>'
            '<div>orange dashed box = restriction (hasValue / cardinality)</div>'
            '<div>thick outline = highly-connected "hub" class</div>'
            '<div>hollow triangle arrow = rdfs:subClassOf</div>'
            '<div>solid arrow = object property, domain &amp; range asserted</div>'
            '<div>dashed arrow = object property, domain/range not asserted</div>'
            '<div>filled/hollow circle on arrow tail = only domain / only range asserted</div>'
            '<div>&#8801; hub = owl:equivalentClass</div>'
            '<div>&#10709; hub = owl:intersectionOf, &#10710; hub = owl:unionOf</div>'
            '<div>&#8869; hub = owl:AllDisjointClasses / disjointWith</div>'
            '<div>&laquo;owl:oneOf&raquo; hexagon = enumeration of individuals</div>'
        )
        self.add_vertex(
            860, -320, 400, 260, legend_value,
            # Deliberately avoids every style substring Chowlk's classifier
            # checks for (shape=note, shape=document, ellipse, hexagon,
            # rhombus, edgeLabel/text, "rounded") so this box is skipped
            # entirely instead of being misread as a class/namespace/metadata
            # element -- "whiteSpace=wrap;html=1" alone is not one of those
            # triggers.
            "whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;"
            "align=left;verticalAlign=top;spacing=8;fontSize=12;",
        )

        # Per-diagram color key: the legend above says "box color = class
        # category" but never says which color IS which category -- without
        # this, a reader sees e.g. green boxes and has no way to know what
        # they have in common. _category_colors is only populated for
        # categories that actually got tinted (>= MIN_CATEGORY_SIZE members),
        # in the same biggest-group-first order emit_classes used, so this
        # key always matches the colors actually on the canvas.
        if self._category_colors:
            key_lines = ['<div><b>Category colors</b></div>']
            for cat, (fill, stroke) in self._category_colors.items():
                label = self.model.classes.get(cat, {}).get("label") or cat
                swatch = (
                    f'<span style="display:inline-block;width:12px;height:12px;'
                    f'margin-right:6px;vertical-align:middle;background:{fill};'
                    f'border:1px solid {stroke};"></span>'
                )
                key_lines.append(f'<div>{swatch}{self.esc(label)}</div>')
            key_value = "".join(key_lines)
            self.add_vertex(
                # To the right of the legend box, sharing its y/height so
                # the whole header band stays above y=0 -- classes start
                # around y=60 (see `margin` in each layout engine), and
                # this header row must never encroach on that or
                # verify_diagram's overlap check will (correctly) reject it.
                1280, -320, 220, min(260, 30 + 22 * len(self._category_colors)), key_value,
                # Same style-avoidance rule as the legend box above.
                "whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#666666;"
                "align=left;verticalAlign=top;spacing=8;fontSize=12;",
            )

    # Chowlk-facing quality gate: sys.exit on any real vertex-vertex overlap
    # or any gap in a class's datatype-property stack, instead of leaving
    # a defect to be discovered by eyeballing the render (this repo has no
    # in-container draw.io renderer -- see CLAUDE.md / README.md's
    # "Validating a generated diagram" section). OVERLAP_TOL stays tight so
    # it precisely catches real overlaps; DP_GAP_TOL is checked separately
    # (rather than reusing OVERLAP_TOL) so the two failure modes stay
    # distinguishable in the error message even though DP_STACK_GAP is 0
    # and both thresholds could technically share one value today.
    OVERLAP_TOL = 2.0
    DP_GAP_TOL = 2.0

    def verify_diagram(self):
        rects = self._vertex_rects
        n = len(rects)
        for i in range(n):
            id_a, xa, ya, wa, ha, la = rects[i]
            for j in range(i + 1, n):
                id_b, xb, yb, wb, hb, lb = rects[j]
                ox = min(xa + wa, xb + wb) - max(xa, xb)
                oy = min(ya + ha, yb + hb) - max(ya, yb)
                if ox > self.OVERLAP_TOL and oy > self.OVERLAP_TOL:
                    sys.exit(
                        f"verify_diagram: layout defect -- vertex {id_a} "
                        f"({la!r} at {xa:.1f},{ya:.1f} {wa}x{ha}) overlaps "
                        f"vertex {id_b} ({lb!r} at {xb:.1f},{yb:.1f} "
                        f"{wb}x{hb}) by {ox:.1f}x{oy:.1f}px. This is a bug "
                        f"in the layout engine, not the ontology -- please "
                        f"report it with the input file and --layout used."
                    )
        for cq, boxes in self._dp_box_rects.items():
            ordered = sorted(boxes, key=lambda r: r[1])
            for (x0, y0, w0, h0), (x1, y1, w1, h1) in zip(ordered, ordered[1:]):
                gap = y1 - (y0 + h0)
                if gap > self.DP_GAP_TOL:
                    sys.exit(
                        f"verify_diagram: gap of {gap:.1f}px in {cq}'s "
                        f"datatype-property stack, between a box ending at "
                        f"y={y0 + h0:.1f} and the next starting at "
                        f"y={y1:.1f}. This is a layout-engine bug, not an "
                        f"ontology problem."
                    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_xml(cells_xml):
    return (
        '<mxfile host="app.diagrams.net" agent="chowlk-generator" version="24.0.0">'
        '<diagram name="ontology" id="ontology-diagram">'
        '<mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" '
        'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1600" pageHeight="1200" '
        'math="0" shadow="0">'
        '<root>'
        '<mxCell id="0"/>'
        '<mxCell id="1" parent="0"/>'
        f'{cells_xml}'
        '</root>'
        '</mxGraphModel>'
        '</diagram>'
        '</mxfile>'
    )


def main():
    args = parse_args()

    if args.edge_routing == "graphviz" and args.layout != "graphviz":
        sys.exit(
            "--edge-routing graphviz requires --layout graphviz: the edge "
            "waypoints are produced by the same `dot` run that positions the "
            "classes, so the node coordinates and the routed paths only line "
            "up under the graphviz layout. Re-run with --layout graphviz, or "
            "use --edge-routing orthogonal for a layout-independent option."
        )
    if args.layout == "elk" and args.edge_routing != "straight":
        # ELK owns edge routing under --layout elk; a conflicting
        # --edge-routing would be silently ignored, so say so rather than
        # pretend it took effect.
        print(
            f"note: --layout elk does its own orthogonal edge routing; "
            f"--edge-routing {args.edge_routing} is ignored.",
            file=sys.stderr,
        )
    if args.elk_mode != "layered" and args.layout != "elk":
        sys.exit("--elk-mode requires --layout elk.")

    g = load_graph(args.input, args.format)
    qname, used_prefixes = make_qname_fn(g)
    model = OntologyModel(g, qname)

    if not model.classes:
        sys.exit(f"No owl:Class instances found in {args.input} -- nothing to draw.")

    diagram = DiagramBuilder(
        model, edge_routing=args.edge_routing, layout=args.layout, elk_mode=args.elk_mode,
    )
    diagram.compute_layout(args.layout)
    diagram.emit_classes()
    diagram.emit_subclass_edges()
    diagram.emit_object_properties()
    diagram.emit_data_properties()
    diagram.emit_equivalent_and_restrictions()
    diagram.emit_disjoint()
    diagram.emit_individuals()
    diagram.emit_notes(args.title, args.input, used_prefixes)
    diagram.verify_diagram()

    xml = build_xml("".join(diagram.cells))

    with open(args.output, "w") as f:
        f.write(xml)

    import xml.dom.minidom as minidom
    minidom.parseString(xml)

    print(f"Parsed {len(g)} triples from {args.input}")
    print(f"Classes: {len(model.classes)}  ObjectProperties: {len(model.obj_props)}  "
          f"DatatypeProperties: {len(model.data_props)}  Individuals: {len(model.individuals)}")
    print(f"Disjoint class sets: {len(model.disjoint_class_sets)}  "
          f"equivalentClass axioms: {sum(len(v) for v in model.equivalent_class_exprs.values())}")
    print(f"Wrote {args.output} ({len(diagram.cells)} mxCells) -- XML is well-formed.")


if __name__ == "__main__":
    main()
