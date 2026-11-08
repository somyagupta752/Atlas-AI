import { describe, expect, it } from "vitest";
import { safeRedirect } from "./safeRedirect";

describe("safeRedirect: the login page only ever returns to a path inside the app", () => {
  it.each([
    ["/", "/"],
    ["/repositories", "/repositories"],
    ["/runs/6f1c/cases/bug-001?variant=live", "/runs/6f1c/cases/bug-001?variant=live"],
    ["/compare?ids=a,b", "/compare?ids=a,b"],
    ["/repositories/my-service#members", "/repositories/my-service#members"],
    ["/search?q=https://example.com", "/search?q=https://example.com"], // a URL as DATA inside the query is fine
  ])("keeps %s", (input, expected) => {
    expect(safeRedirect(input)).toBe(expected);
  });

  it.each([
    "https://evil.example/",
    "http://evil.example",
    "//evil.example",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "\\/evil.example",
    "/%5Cevil.example", // encoded backslash
    "/%5c%5cevil.example",
    "/%2F/evil.example", // encoded slash makes it protocol-relative once decoded
    "/%2f%2fevil.example",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "/\t/evil.example",
    "/\n/evil.example",
    "/%09/evil.example",
    "/%0a/evil.example",
    "/%",
    " //evil.example",
    "evil.example",
    "",
    null,
    undefined,
    42,
    {},
    ["/ok"],
    "/" + "a".repeat(2100),
  ])("rejects %j", (input) => {
    expect(safeRedirect(input)).toBe("/");
  });

  it("uses the fallback it is given", () => {
    expect(safeRedirect("//evil.example", "/repositories")).toBe("/repositories");
  });

  it("never sends a signed-in user back to a sign-in page", () => {
    for (const page of ["/login", "/login?next=/x", "/register", "/reset-password#token=abc"]) {
      expect(safeRedirect(page)).toBe("/");
    }
    expect(safeRedirect("/loginhelp")).toBe("/loginhelp"); // only the exact auth pages are excluded
  });
});
