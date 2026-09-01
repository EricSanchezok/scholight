"use strict";

// Batch KaTeX renderer used by scholight.survey.katex_render.
//
// Reads one JSON object from stdin:
//   {"formulas": [{"id": 0, "tex": "\\frac{a}{b}", "display": false}, ...]}
// Writes one JSON object to stdout:
//   {"results": [{"id": 0, "html": "<span class=...>...</span>"}],
//    "errors":  [{"id": 3, "message": "Parse error: ..."}]}
//
// A single formula failure never aborts the batch: the error is reported
// under that id and the caller falls back to a text span.

const katex = require("./katex.min.js");

function main() {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    input += chunk;
  });
  process.stdin.on("end", () => {
    let request;
    try {
      request = JSON.parse(input);
    } catch (error) {
      process.stdout.write(JSON.stringify({ results: [], errors: [] }));
      process.exit(2);
    }
    const results = [];
    const errors = [];
    for (const formula of request.formulas || []) {
      try {
        const html = katex.renderToString(formula.tex, {
          output: "html",
          throwOnError: true,
          displayMode: Boolean(formula.display),
          strict: false,
          trust: false,
        });
        results.push({ id: formula.id, html });
      } catch (error) {
        errors.push({
          id: formula.id,
          message: String(error && error.message ? error.message : error),
        });
      }
    }
    // Exit only after stdout has fully flushed: process.exit() truncates
    // output once it exceeds the ~64 KiB pipe buffer, which would corrupt
    // the JSON response for large batches.
    process.stdout.write(JSON.stringify({ results, errors }), () => {
      process.exit(0);
    });
  });
}

main();
