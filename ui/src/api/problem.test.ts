import { describe, expect, it } from "vitest";
import type { RuntimeConfig } from "@/config";
import { http, HttpResponse, server } from "@/test/msw";
import { createApiClient } from "./client";
import { mapFieldErrors } from "./fieldErrors";
import {
  ApiError,
  copyFor,
  isApiError,
  networkProblem,
  parseProblem,
  type HttpProblem,
  type Problem,
} from "./problem";

const config: RuntimeConfig = {
  entraEnabled: false,
  entraClientId: "",
  entraAuthority: "",
  entraScopes: [],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

function problemResponse(
  body: unknown,
  status: number,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/problem+json", ...headers },
  });
}

function problem(overrides: Partial<HttpProblem> = {}): HttpProblem {
  return {
    kind: "problem",
    status: 400,
    slug: null,
    title: "Bad request",
    detail: null,
    errors: [],
    requestId: null,
    ...overrides,
  };
}

async function caught(call: () => Promise<unknown>): Promise<unknown> {
  try {
    await call();
  } catch (error) {
    return error;
  }
  throw new Error("expected the call to throw");
}

describe("parseProblem", () => {
  it("reads slug, title, detail, errors and the request id", async () => {
    const parsed = await parseProblem(
      problemResponse(
        {
          type: "urn:dc:problem:validation-error",
          title: "Validation failed",
          detail: "The body is invalid.",
          errors: [
            { pointer: "/new_password", detail: "Too short." },
            { pointer: 7, detail: "dropped: pointer is not a string" },
            { pointer: "/x" },
          ],
          stack: "Traceback (most recent call last)",
        },
        422,
        { "X-Request-ID": "r-42" },
      ),
    );
    expect(parsed).toEqual({
      kind: "problem",
      status: 422,
      slug: "validation-error",
      title: "Validation failed",
      detail: "The body is invalid.",
      errors: [{ pointer: "/new_password", detail: "Too short." }],
      requestId: "r-42",
    });
  });

  it("keeps the ids of an odcs-id-mismatch and drops unknown members", async () => {
    const parsed = await parseProblem(
      problemResponse(
        {
          type: "urn:dc:problem:odcs-id-mismatch",
          title: "Unprocessable Content",
          expected_odcs_id: "urn:a:b",
          found_odcs_id: "urn:a:c",
          stack: "never copied",
        },
        422,
      ),
    );
    expect(parsed.extensions).toEqual({
      expected_odcs_id: "urn:a:b",
      found_odcs_id: "urn:a:c",
    });
  });

  it("accepts application/json as well", async () => {
    const parsed = await parseProblem(
      new Response(
        JSON.stringify({
          type: "urn:dc:problem:not-found",
          title: "Not found",
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(parsed.slug).toBe("not-found");
  });

  it.each([
    ["about:blank"],
    ["urn:other:thing"],
    ["urn:dc:problem:"],
    ["urn:dc:problem:Bad Slug<script>"],
    [undefined],
  ])(
    "gives a null slug for type %s and keeps title and detail",
    async (type) => {
      const parsed = await parseProblem(
        problemResponse(
          { type, title: "Teapot", detail: "Short and stout." },
          418,
        ),
      );
      expect(parsed.slug).toBeNull();
      expect(parsed.title).toBe("Teapot");
      expect(parsed.detail).toBe("Short and stout.");
    },
  );

  it("never copies a non-JSON body into title or detail", async () => {
    const parsed = await parseProblem(
      new Response("<html>nginx 502 Bad Gateway at /srv/secret</html>", {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
    );
    expect(parsed.title).toBe("Something went wrong");
    expect(parsed.detail).toBeNull();
    expect(parsed.slug).toBeNull();
    expect(JSON.stringify(parsed)).not.toContain("nginx");
  });

  it("survives a JSON content type with an unparseable or non-object body", async () => {
    const broken = await parseProblem(
      new Response("{not json", {
        status: 500,
        headers: { "Content-Type": "application/problem+json" },
      }),
    );
    expect(broken.title).toBe("Something went wrong");
    const array = await parseProblem(problemResponse(["x"], 500));
    expect(array.detail).toBeNull();
  });

  it("omits a missing or malformed request id", async () => {
    expect((await parseProblem(problemResponse({}, 500))).requestId).toBeNull();
    const odd = await parseProblem(
      problemResponse({}, 500, { "X-Request-ID": "<b>no</b>" }),
    );
    expect(odd.requestId).toBeNull();
  });

  it("uses a fixed title per status when the body has none", async () => {
    expect((await parseProblem(problemResponse({}, 503))).title).toBe(
      "Service temporarily unavailable",
    );
    expect((await parseProblem(problemResponse({}, 404))).title).toBe(
      "Not found",
    );
    expect((await parseProblem(problemResponse({}, 429))).title).toBe(
      "Too many requests",
    );
  });

  it("falls back to the generic title for a body-less response without a content type", async () => {
    const parsed = await parseProblem(new Response(null, { status: 500 }));
    expect(parsed).toMatchObject({
      title: "Something went wrong",
      detail: null,
    });
  });

  it("treats blank text as absent and skips malformed field errors", async () => {
    const parsed = await parseProblem(
      problemResponse(
        {
          title: "   ",
          detail: "",
          errors: [
            "not an object",
            null,
            { pointer: "/name", detail: "  " },
            { pointer: 7, detail: "Pointer is not a string." },
            { pointer: "/slug", detail: "Already taken." },
          ],
        },
        409,
      ),
    );
    expect(parsed.title).toBe("Something went wrong");
    expect(parsed.detail).toBeNull();
    expect(parsed.errors).toEqual([
      { pointer: "/slug", detail: "Already taken." },
    ]);
  });
});

describe("createApiClient problem handling", () => {
  it("throws an ApiError carrying the parsed problem for a non-2xx", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          {
            type: "urn:dc:problem:csrf-check-failed",
            title: "CSRF check failed",
            detail: "Missing header.",
          },
          {
            status: 403,
            headers: {
              "Content-Type": "application/problem+json",
              "X-Request-ID": "r-1",
            },
          },
        ),
      ),
    );
    const client = createApiClient(config);
    const error = await caught(() => client.GET("/api/v1/identity/me"));
    expect(isApiError(error)).toBe(true);
    const { problem: parsed } = error as ApiError;
    expect(parsed).toMatchObject({
      kind: "problem",
      status: 403,
      slug: "csrf-check-failed",
      requestId: "r-1",
    });
  });

  it("turns a non-JSON 502 into a generic problem", async () => {
    server.use(
      http.get(
        "*/api/v1/identity/me",
        () => new HttpResponse("upstream exploded", { status: 502 }),
      ),
    );
    const error = await caught(() =>
      createApiClient(config).GET("/api/v1/identity/me"),
    );
    expect((error as ApiError).problem).toMatchObject({
      status: 502,
      title: "Something went wrong",
      slug: null,
    });
  });

  it("turns a rejected fetch into a network problem", async () => {
    const client = createApiClient(config, {
      fetch: () => Promise.reject(new TypeError("Failed to fetch")),
    });
    const error = await caught(() => client.GET("/api/v1/identity/me"));
    expect(isApiError(error)).toBe(true);
    expect((error as ApiError).problem).toEqual(networkProblem());
  });

  it("leaves an aborted request as the abort", async () => {
    const client = createApiClient(config, {
      fetch: () => Promise.reject(new DOMException("aborted", "AbortError")),
    });
    const error = await caught(() => client.GET("/api/v1/identity/me"));
    expect(isApiError(error)).toBe(false);
    expect((error as DOMException).name).toBe("AbortError");
  });
});

describe("copyFor", () => {
  it("answers a sign-in invalid-credentials with the fixed sentence", () => {
    expect(
      copyFor(problem({ status: 401, slug: "invalid-credentials" }), "signin"),
    ).toBe("Username or password is incorrect.");
  });

  it("uses the throttle copy for any 429", () => {
    expect(
      copyFor(problem({ status: 429, slug: "too-many-attempts" }), "signin"),
    ).toBe("Too many attempts, try again in a moment.");
    expect(copyFor(problem({ status: 429 }), "page")).toBe(
      "Too many attempts, try again in a moment.",
    );
  });

  it("uses the not-permitted copy for a 403 on an action only", () => {
    expect(copyFor(problem({ status: 403, slug: "forbidden" }), "action")).toBe(
      "You are not permitted to do this here. Ask a folder admin for access.",
    );
    expect(
      copyFor(problem({ status: 403, slug: "forbidden" }), "page"),
    ).toBeNull();
  });

  it("uses the unavailable copy for 503 and the network copy for a network problem", () => {
    expect(copyFor(problem({ status: 503 }), "page")).toBe(
      "Service temporarily unavailable",
    );
    expect(copyFor(networkProblem(), "page")).toBe("Can't reach the server");
  });

  it("returns null for an unknown slug", () => {
    expect(
      copyFor(problem({ status: 409, slug: "something-new" }), "action"),
    ).toBeNull();
  });
});

describe("mapFieldErrors", () => {
  const withErrors: Problem = problem({
    status: 422,
    slug: "validation-error",
    errors: [
      { pointer: "/new_password", detail: "Too common." },
      { pointer: "/unknown", detail: "Not a field." },
    ],
  });

  it("puts a known pointer on its field and returns the rest", () => {
    expect(
      mapFieldErrors(withErrors, { "/new_password": "newPassword" }),
    ).toEqual({
      fieldErrors: { newPassword: "Too common." },
      unmapped: [{ pointer: "/unknown", detail: "Not a field." }],
    });
  });

  it("keeps a second error for a filled field visible", () => {
    const twice = problem({
      errors: [
        { pointer: "/a", detail: "First." },
        { pointer: "/a", detail: "Second." },
      ],
    });
    expect(mapFieldErrors(twice, { "/a": "a" })).toEqual({
      fieldErrors: { a: "First." },
      unmapped: [{ pointer: "/a", detail: "Second." }],
    });
  });

  it("does not match Object.prototype names and ignores a network problem", () => {
    const proto = problem({
      errors: [{ pointer: "constructor", detail: "x" }],
    });
    expect(mapFieldErrors(proto, {}).unmapped).toHaveLength(1);
    expect(mapFieldErrors(networkProblem(), { "/a": "a" })).toEqual({
      fieldErrors: {},
      unmapped: [],
    });
  });
});
