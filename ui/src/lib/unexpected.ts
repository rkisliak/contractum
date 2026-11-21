import { isApiError, type Problem } from "@/api/problem";

// Anything that is not an ApiError (a bug, not a server answer) still gets the generic alert.
export const UNEXPECTED_PROBLEM: Problem = {
  kind: "problem",
  status: 500,
  slug: null,
  title: "Something went wrong",
  detail: null,
  errors: [],
  requestId: null,
};

/** The Problem to render for any query or mutation error. */
export function problemOf(error: unknown): Problem {
  return isApiError(error) ? error.problem : UNEXPECTED_PROBLEM;
}

/** True when the error is an API 404 (render NotFound, never "forbidden"). */
export function isNotFound(error: unknown): boolean {
  return isApiError(error) && error.problem.status === 404;
}
