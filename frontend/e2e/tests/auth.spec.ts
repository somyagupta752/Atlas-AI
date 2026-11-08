import { apiClient, expect, expectSignedIn, expectSignedOut, newAccount, PASSWORD, readableState, refreshCookie, registerViaApi, signIn, test } from "../support/fixtures";

test.describe("registration and sign-in (real frontend, real API)", () => {
  test("registering signs the person in, and the browser keeps no readable credential", async ({ page, context }) => {
    const account = newAccount("register");
    const requests: { url: string; cookie: string }[] = [];
    page.on("requestfinished", async (r) => {
      if (/\/(repositories|evaluations|auth\/[a-z-]+)(\?|$)/.test(new URL(r.url()).pathname + new URL(r.url()).search)) {
        requests.push({ url: new URL(r.url()).pathname, cookie: (await r.allHeaders())["cookie"] ?? "" });
      }
    });

    await page.goto("/register");
    await page.getByLabel("Email").fill(account.email);
    await page.getByLabel(/^Password/).fill(account.password);
    await page.getByLabel("Repeat the password").fill(account.password);
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expectSignedIn(page, account);
    expect(new URL(page.url()).pathname).toBe("/repos");

    // The refresh token is an httpOnly, same-site cookie scoped to /auth ...
    const cookie = await refreshCookie(context);
    expect(cookie, "the server set a refresh cookie").toBeDefined();
    expect(cookie).toMatchObject({ httpOnly: true, sameSite: "Strict", path: "/auth" });
    expect(cookie!.expires).toBeGreaterThan(Date.now() / 1000 + 13 * 86400); // about 14 days
    // ... so page scripts cannot read it, and nothing else holds a credential.
    const seen = await readableState(page);
    expect(seen.cookie).not.toContain("atlas_refresh");
    expect(seen.local).toBe("{}");
    expect(seen.session).toBe("{}");
    expect(seen.url).not.toMatch(/token|eyJ/);
    // The cookie travels only to /auth, never with a data request.
    const data = requests.filter((r) => /^\/(repositories|evaluations)$/.test(r.url));
    expect(data.length).toBeGreaterThan(0);
    for (const r of data) expect(r.cookie).not.toContain("atlas_refresh");
    expect(requests.filter((r) => r.url === "/auth/login").length).toBe(1);
  });

  test("a duplicate email is refused with a clear message and creates nothing", async ({ page, playwright, web, address }) => {
    const account = newAccount("duplicate");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await page.goto("/register");
    await page.getByLabel("Email").fill(account.email.toUpperCase()); // emails are case-insensitive
    await page.getByLabel(/^Password/).fill(account.password);
    await page.getByLabel("Repeat the password").fill(account.password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("alert")).toContainText("already exists");
    await expect(page.getByRole("heading", { name: "Create an account" })).toBeVisible();
  });

  test("short and mismatched passwords are stopped in the browser before any request is made", async ({ page }) => {
    let registerCalls = 0;
    page.on("request", (r) => { if (r.url().endsWith("/auth/register")) registerCalls++; });
    await page.goto("/register");
    await page.getByLabel("Email").fill(newAccount().email);
    await page.getByLabel(/^Password/).fill("too short");
    await expect(page.getByRole("alert")).toContainText("at least 12");
    await page.getByLabel(/^Password/).fill(PASSWORD);
    await page.getByLabel("Repeat the password").fill(PASSWORD + "x");
    await expect(page.getByRole("alert")).toContainText("do not match");
    await expect(page.getByRole("button", { name: "Create account" })).toBeDisabled();
    expect(registerCalls).toBe(0);
  });

  test("the server's own password policy is enforced even if the browser's check is bypassed", async ({ page, playwright, web, address }) => {
    const api = await apiClient(playwright, web, address);
    for (const weak of ["aaaaaaaaaaaaaaaa", "short", "x".repeat(200)]) {
      const response = await api.post("/auth/register", { data: { email: newAccount().email, password: weak } });
      expect(response.status(), weak).toBe(422);
    }
    expect((await api.post("/auth/register", { data: { email: "not-an-email", password: PASSWORD } })).status()).toBe(422);
    await page.goto("/login"); // nothing was created, nobody is signed in
    await expectSignedOut(page);
  });

  test("a wrong password and an unknown email look identical and leave no session", async ({ page, context, playwright, web, address }) => {
    const account = newAccount("wrong");
    await registerViaApi(await apiClient(playwright, web, address), account);

    await page.goto("/login");
    await page.getByLabel("Email").fill(account.email);
    await page.getByLabel("Password").fill("not the password at all");
    await page.getByRole("button", { name: "Sign in" }).click();
    const known = await page.getByRole("alert").textContent();

    await page.getByLabel("Email").fill(newAccount("ghost").email);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toHaveText(known!);
    expect(known).toContain("Incorrect email or password");
    expect(await refreshCookie(context)).toBeUndefined();
    await expectSignedOut(page);
  });

  test("repeated failures are rate limited, and the page says how long to wait", async ({ page, playwright, web, address }) => {
    const account = newAccount("ratelimit");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await page.goto("/login");
    await page.getByLabel("Email").fill(account.email);
    await page.getByLabel("Password").fill("wrong password number");
    for (let attempt = 0; attempt < 5; attempt++) {
      await page.getByRole("button", { name: "Sign in" }).click();
      await expect(page.getByRole("alert")).toContainText("Incorrect email or password");
    }
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText(/Too many attempts\. Try again in \d+ seconds/);
    // Even the right password is refused while blocked, so guessing cannot continue by luck.
    await page.getByLabel("Password").fill(account.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText("Too many attempts");
    await expectSignedOut(page);
  });

  test("signing in shows the signed-in user and the navigation", async ({ page, playwright, web, address }) => {
    const account = newAccount("login");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
    await expectSignedIn(page, account);
    for (const link of ["Overview", "Repositories", "Start evaluation", "Compare"]) {
      await expect(page.getByRole("link", { name: link, exact: true })).toBeVisible();
    }
    // Already signed in: the sign-in page sends you on instead of showing a form.
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
  });
});

test.describe("protected routes and redirects", () => {
  test("every page requires a session, and sign-in returns to the page that was asked for", async ({ page, playwright, web, address }) => {
    const account = newAccount("protected");
    await registerViaApi(await apiClient(playwright, web, address), account);
    for (const path of ["/", "/repos", "/repos/anything", "/start", "/compare", "/runs/00000000-0000-0000-0000-000000000000"]) {
      await page.goto(path);
      await expectSignedOut(page);
      expect(new URL(page.url()).pathname).toBe("/login");
      expect(new URL(page.url()).searchParams.get("next")).toBe(path);
    }
    await signIn(page, account, "/compare?ids=a,b");
    await expect(page.getByRole("heading", { name: "Compare evaluations" })).toBeVisible();
    expect(page.url()).toContain("/compare?ids=a,b");
  });

  test("a page reload keeps the session (the cookie is exchanged for a new access token)", async ({ page, playwright, web, address }) => {
    const account = newAccount("reload");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await expectSignedIn(page, account);
    const refreshed = page.waitForResponse((r) => r.url().endsWith("/auth/refresh") && r.status() === 200);
    await page.reload();
    await refreshed;
    await expectSignedIn(page, account);
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
  });

  test("deep links survive a reload: page routes do not collide with API paths", async ({ page, playwright, web, address }) => {
    const account = newAccount("deeplink");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await page.goto("/repos"); // a full page load of the repositories page, not a client-side navigation
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    expect((await page.content()).startsWith("{")).toBe(false); // not the API's JSON
  });

  for (const hostile of [
    "//evil.example/steal",
    "https://evil.example/steal",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(document.domain)",
    "/%2F/evil.example",
    "/%5Cevil.example",
    "data:text/html,<script>alert(1)</script>",
  ]) {
    test(`a hostile post-sign-in target is ignored: ${hostile}`, async ({ page, context, playwright, web, address }) => {
      const account = newAccount("redirect");
      await registerViaApi(await apiClient(playwright, web, address), account);
      let leftTheSite = false;
      // Match the HOST only: the hostile text is also (harmlessly) in our own login URL's query string.
      await context.route((url) => url.hostname === "evil.example", (route) => { leftTheSite = true; return route.abort(); });
      page.on("dialog", async (d) => { leftTheSite = true; await d.dismiss(); }); // javascript: would raise an alert

      await signIn(page, account, hostile);
      await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
      expect(new URL(page.url()).origin).toBe(new URL(web).origin);
      expect(new URL(page.url()).pathname).toBe("/");
      expect(leftTheSite).toBe(false);
    });
  }

  test("sign-in pages are never a redirect target", async ({ page, playwright, web, address }) => {
    const account = newAccount("loop");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account, "/register");
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
  });
});

test.describe("signing out", () => {
  test("ends the session in the browser and on the server, and locks the pages", async ({ page, context, playwright, web, address }) => {
    const account = newAccount("logout");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await expectSignedIn(page, account);
    const before = await refreshCookie(context);
    expect(before).toBeDefined();
    await page.getByRole("link", { name: "Repositories", exact: true }).click(); // a real history entry to come back to
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();

    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expectSignedOut(page);
    expect(await refreshCookie(context)).toBeUndefined();

    // The pages are locked, including through the Back button: the page you were on is not shown again.
    await page.goBack();
    await expectSignedOut(page);
    await expect(page.getByRole("heading", { name: "Repositories" })).toHaveCount(0);
    await page.goto("/repos");
    await expectSignedOut(page);

    // The old cookie value is dead on the server too: a copy of it cannot start a session.
    const replay = await playwright.request.newContext({
      baseURL: web, extraHTTPHeaders: { "X-Forwarded-For": address, Cookie: `atlas_refresh=${before!.value}` } });
    expect((await replay.post("/auth/refresh")).status()).toBe(401);
  });
});
