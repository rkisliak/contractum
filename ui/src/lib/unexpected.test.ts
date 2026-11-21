import { describe, expect, it } from "vitest";
import { ApiError, networkProblem, type HttpProblem } from "@/api/problem";
import { isNotFound, problemOf, UNEXPECTED_PROBLEM } from "./unexpected";

function httpProblem(status: number): HttpProblem {
  return {
    kind: "problem",
    status,
    slug: null,
    title: `Status ${String(status)}`,
    detail: null,
    errors: [],
    requestId: "req-1",
  };
}

describe("problemOf", () => {
  it("returns the problem an ApiError carries", () => {
    const problem = httpProblem(409);
    expect(problemOf(new ApiError(problem))).toBe(problem);
    expect(problemOf(new ApiError(networkProblem()))).toEqual(networkProblem());
  });

  it("returns the generic problem for anything that is not an ApiError", () => {
    expect(problemOf(new TypeError("x is undefined"))).toBe(UNEXPECTED_PROBLEM);
    expect(problemOf("boom")).toBe(UNEXPECTED_PROBLEM);
    expect(UNEXPECTED_PROBLEM).toMatchObject({
      status: 500,
      title: "Something went wrong",
    });
  });
});

describe("isNotFound", () => {
  it("is true only for an API 404", () => {
    expect(isNotFound(new ApiError(httpProblem(404)))).toBe(true);
    expect(isNotFound(new ApiError(httpProblem(403)))).toBe(false);
    expect(isNotFound(new ApiError(networkProblem()))).toBe(false);
    expect(isNotFound(new Error("404"))).toBe(false);
  });
});
