import { describe, expect, it } from "vitest";

import type { SearchHit } from "../api/types";
import {
  buildSearchUrl,
  citationFor,
  dateFromPreset,
  formatAuthors,
  parseSearchParameters,
  searchResultBylineParts,
  searchResultMetadataParts,
} from "./format";

const hit: SearchHit = {
  rank: 1,
  score: 12.75,
  arxiv_id: "2401.12345",
  title: "A Paper About Retrieval",
  authors: ["Ada Lovelace", "Alan Turing", "Grace Hopper"],
  abstract: "Abstract",
  categories: ["cs.IR"],
  submitted_at: "2024-01-20T00:00:00Z",
  updated_at: "2024-03-05T00:00:00Z",
  version: 2,
  arxiv_url: "https://arxiv.org/abs/2401.12345",
  pdf_url: "https://arxiv.org/pdf/2401.12345",
};

describe("search presentation helpers", () => {
  it("formats three or more authors as first author et al", () => {
    expect(formatAuthors(hit.authors)).toBe("Ada Lovelace et al.");
  });

  it("builds the documented plain-text citation", () => {
    expect(citationFor(hit)).toBe(
      "Ada Lovelace, Alan Turing, Grace Hopper (2024). A Paper About Retrieval. arXiv:2401.12345. https://arxiv.org/abs/2401.12345",
    );
  });

  it("formats missing paper metadata without invalid dates or dangling separators", () => {
    const sparseHit: SearchHit = {
      ...hit,
      authors: [],
      categories: [],
      submitted_at: null,
      updated_at: null,
      version: null,
    };

    expect(searchResultBylineParts(sparseHit)).toEqual(["Unknown authors", "arXiv:2401.12345"]);
    expect(
      searchResultMetadataParts(sparseHit, undefined, {
        submitted: "Submitted",
        score: "Score",
      }),
    ).toEqual(["Score 12.750"]);
    expect(citationFor(sparseHit)).toBe(
      "Unknown authors (n.d.). A Paper About Retrieval. arXiv:2401.12345. https://arxiv.org/abs/2401.12345",
    );
  });

  it("round-trips hidden search filters through the URL", () => {
    const url = buildSearchUrl({
      query: "retrieval",
      strength: "thorough",
      limit: 30,
      filters: {
        categories: ["cs.IR", "cs.AI"],
        authors: ["Lovelace"],
        date_from: "2020-01-01",
        date_to: "2025-01-01",
      },
    });
    const parsed = parseSearchParameters(new URLSearchParams(url.split("?")[1]));
    expect(parsed).toEqual({
      query: "retrieval",
      strength: "thorough",
      limit: 30,
      filters: {
        categories: ["cs.IR", "cs.AI"],
        authors: ["Lovelace"],
        date_from: "2020-01-01",
        date_to: "2025-01-01",
      },
    });
  });

  it("maps relative date presets to an inclusive UTC lower bound", () => {
    expect(dateFromPreset("6months", new Date("2026-07-27T18:00:00Z"))).toBe("2026-01-27");
  });
});
