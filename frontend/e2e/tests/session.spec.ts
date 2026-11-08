import type { Page, Response } from "@playwright/test";
import { ageSpentRefreshTokens, refreshTokenCount } from "../support/db";
import { apiClient, apiLogin, expect, expectSignedIn, expectSignedOut, newAccount, newSession, refreshCookie, registerViaApi, signIn, test } from "../support/fixtures";
import { expiredAccessToken, forgedAccessToken } from "../support/jwt";

// Replace the access token on the NEXT request to the repositories API with `token`, to simulate a token that timed out. The
// server still checks the signature and expiry for real, so the 401 -> renew -> retry path is the genuine one.
async function swapTokenOnce(page: Page, token: string): Promise<{ swapped: () => boolean; token: string }> {
  let done = false;
  await page.route("**/repositories", async (route) => {
    if (done || route.request().method() !== "GET") return route.continue();
    done = true;
    await route.continue({ headers: { ...route.request().headers(), authorization: `Bearer ${token}` } });
  });
  return { swapped: () => done, token };
}

function watchApi(page: Page) {
  const seen: { path: string; status: number; auth: string }[] = [];
  page.on("response", async (r: Response) => {
    const url = new URL(r.url());
    if (/^\/(repositories|auth\/refresh)$/.test(url.pathname) && r.request().method() !== "OPTIONS") {
      seen.push({ path: url.pathname, status: r.status(), auth: r.request().headers()["authorization"] ?? "" });
    }
  });
  return seen;
}

test.describe("access-token expiry and renewal", () => {
  test("an expired token is renewed from the cookie and the request is repeated, invisibly to the user", async ({ page, playwright, web, address }) => {
    const account = newAccount("expiry");
    const userId = await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    const seen = watchApi(page);
    const hook = await swapTokenOnce(page, expiredAccessToken(userId));

    await page.getByRole("link", { name: "Repositories", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expect(page.getByText("You have no repositories yet")).toBeVisible(); // the repeated request succeeded
    expect(hook.swapped()).toBe(true);

    const data = seen.filter((s) => s.path === "/repositories");
    expect(data.map((s) => s.status)).toEqual([401, 200]);
    expect(data[0].auth).toBe(`Bearer ${hook.token}`); // the first attempt carried the expired token
    expect(data[1].auth).not.toBe(data[0].auth);
    expect(seen.filter((s) => s.path === "/auth/refresh")).toHaveLength(1); // exactly one renewal
    await expectSignedIn(page, account);
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("the API itself refuses an expired token, a token signed with another key, and garbage", async ({ playwright, web, address }) => {
    const account = newAccount("tokens");
    const api = await apiClient(playwright, web, address);
    const userId = await registerViaApi(api, account);
    const { token } = await apiLogin(api, account);
    for (const bad of [expiredAccessToken(userId), forgedAccessToken(userId), "garbage", "a.b.c", ""]) {
      const response = await (await apiClient(playwright, web, address, bad || undefined)).get("/repositories");
      expect(response.status(), bad.slice(0, 20)).toBe(401);
    }
    expect((await (await apiClient(playwright, web, address, token)).get("/repositories")).status()).toBe(200);
  });

  test("a wrong-key token in the browser also ends in a transparent renewal, not a lock-out", async ({ page, playwright, web, address }) => {
    const account = newAccount("forged");
    const userId = await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await swapTokenOnce(page, forgedAccessToken(userId));
    await page.getByRole("link", { name: "Repositories", exact: true }).click();
    await expect(page.getByText("You have no repositories yet")).toBeVisible();
    await expectSignedIn(page, account);
  });
});

test.describe("session failure and recovery", () => {
  test("when the refresh cookie is gone, the user is told the session expired and can sign in again where they were", async ({ page, context, playwright, web, address }) => {
    const account = newAccount("lost");
    const userId = await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await page.getByRole("link", { name: "Repositories", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();

    await context.clearCookies({ name: "atlas_refresh" }); // the cookie vanishes (cleared, expired, blocked)
    await swapTokenOnce(page, expiredAccessToken(userId));
    await page.getByRole("link", { name: "Overview", exact: true }).click();
    await page.getByRole("link", { name: "Repositories", exact: true }).click();

    await expectSignedOut(page);
    await expect(page.getByRole("status")).toContainText("Your session has expired");
    expect(new URL(page.url()).searchParams.get("next")).toBe("/repos");
    await expect(page.getByText(account.email, { exact: true })).toHaveCount(0);

    await page.getByLabel("Email").fill(account.email); // recovery: sign in once more
    await page.getByLabel("Password").fill(account.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/repos");
  });

  test("a server that cannot be reached at page load is reported as such, and the session is intact afterwards", async ({ page, playwright, web, address }) => {
    const account = newAccount("offline");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);

    await page.route("**/auth/refresh", (route) => route.abort("connectionrefused"));
    await page.reload();
    await expectSignedOut(page);
    await expect(page.getByRole("status")).toContainText("cannot be reached"); // not "expired": nothing was revoked

    await page.unroute("**/auth/refresh");
    await page.goto("/");
    await expectSignedIn(page, account); // the cookie was never touched
  });

  test("a stolen, already-used refresh token is rejected, and using it ends the whole login", async ({ page, context, playwright, web, address }) => {
    const account = newAccount("theft");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    const stolen = (await refreshCookie(context))!.value;

    await page.reload(); // the real user's browser rotates the token: `stolen` is now spent
    await expectSignedIn(page, account);
    const current = (await refreshCookie(context))!.value;
    expect(current).not.toBe(stolen);

    await ageSpentRefreshTokens(account.email); // let the 10-second concurrent-request allowance pass
    const thief = await playwright.request.newContext({
      baseURL: web, extraHTTPHeaders: { "X-Forwarded-For": address, Cookie: `atlas_refresh=${stolen}` } });
    expect((await thief.post("/auth/refresh")).status()).toBe(401);

    // The server treated the replay as theft and ended the login: the genuine browser is signed out too.
    expect(await refreshTokenCount(account.email)).toMatchObject({ live: 0 });
    await page.reload();
    await expectSignedOut(page);
  });

  test("two tabs reloading at the same moment are not mistaken for theft", async ({ context, page, playwright, web, address }) => {
    const account = newAccount("tabs");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    const second = await context.newPage(); // a new tab starts with no access token: it must renew from the cookie
    await second.goto("/");
    await expectSignedIn(second, account);

    await Promise.all([page.reload(), second.reload()]); // both present the same cookie within moments
    await expectSignedIn(page, account);
    await expectSignedIn(second, account);
    await Promise.all([page.reload(), second.reload()]);
    await expectSignedIn(page, account);
    await expectSignedIn(second, account);
    expect((await refreshTokenCount(account.email)).live).toBeGreaterThan(0);
  });
});

test.describe("signing out everywhere", () => {
  test("one click ends the session on every device, immediately, not after the 15-minute token expires", async ({ browser, page, playwright, web, address }) => {
    const account = newAccount("everywhere");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account); // device A
    const other = await newSession(browser, web, address); // device B: its own cookie jar
    await signIn(other.page, account);
    await other.page.getByRole("link", { name: "Repositories", exact: true }).click();
    await expect(other.page.getByRole("heading", { name: "Repositories" })).toBeVisible();

    await page.waitForTimeout(1200); // tokens carry whole-second issue times; revocation applies to earlier seconds
    await page.getByRole("button", { name: "Sign out everywhere" }).click();
    await expectSignedOut(page);
    await expect(page.getByRole("status")).toContainText("Signed out on every device");
    expect(await refreshCookie(page.context())).toBeUndefined();

    // Device B never clicked anything. Its next action finds the session gone, even though its own access token had
    // not expired yet and its refresh cookie is still in its jar.
    expect(await refreshCookie(other.context)).toBeDefined();
    await other.page.getByRole("link", { name: "Overview", exact: true }).click();
    await expectSignedOut(other.page);
    await expect(other.page.getByRole("status")).toContainText("Your session has expired");
    expect(await refreshTokenCount(account.email)).toMatchObject({ live: 0 });
    await other.context.close();

    // Signing in again works.
    await signIn(page, account);
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
  });
});
