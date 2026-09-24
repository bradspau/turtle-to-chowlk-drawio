#!/usr/bin/env node
//
// elk_layout.js -- layout helper for gen_drawio.py's `--layout elk` mode.
//
// Reads an ELK graph as JSON on stdin, runs the Eclipse Layout Kernel
// (elkjs) layered algorithm with orthogonal edge routing, and writes the
// laid-out graph back as JSON on stdout (nodes gain x/y; edges gain
// `sections` with startPoint/bendPoints/endPoint; edge labels gain x/y).
//
// gen_drawio.py shells out to `node elk_layout.js` and reads the result --
// see DiagramBuilder._compute_layout_elk. The Python side supplies all
// layout options and node/label dimensions; this script is a thin, generic
// wrapper that adds no ontology-specific knowledge.
//
// Requires elkjs to be installed alongside this file:
//     cd script && npm install
//
const ELK = require("elkjs");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  let graph;
  try {
    graph = JSON.parse(input);
  } catch (e) {
    process.stderr.write("elk_layout.js: invalid JSON on stdin: " + e.message + "\n");
    process.exit(1);
  }
  const elk = new ELK();
  elk
    .layout(graph)
    .then((laid) => {
      process.stdout.write(JSON.stringify(laid));
    })
    .catch((e) => {
      process.stderr.write("elk_layout.js: ELK layout failed: " + (e && e.message ? e.message : e) + "\n");
      process.exit(1);
    });
});
