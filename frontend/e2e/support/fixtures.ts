import { test as base, expect, type APIRequestContext, type Browser, type BrowserContext, type Page, type TestInfo } from "@playwright/test";
import { E2E_EMAIL_DOMAIN } from "./db";

export const PASSWORD = "e2e correct horse battery staple";
export { expect };

export interface Account {
  email: string;
  password: string;
}

// Each test gets its own client address (X-Forwarded-For, which the test API trusts from the local proxy), so the API's
// per-address rate limits cannot make one test fail because another one used up the allowance.
export function addressFor(info: TestInfo): string {
  let h = 2166136261;
  for (const ch of `${info.testId}:${info.workerIndex}`) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return `10.${(h >>> 16) & 255}.${(h >>> 8) & 255}.${(h & 255) || 1}`;
}

let counter = 0;
export function newAccount(label = "user"): Account {
  const id = `${Date.now().toString(36)}${(counter++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return { email: `${label}-${id}@${E2E_EMAIL_DOMAIN}`, password: PASSWORD };
}

type Fixtures = {
  address: string;
  // Console errors fail a test, except the browser's own report of an HTTP error status, which several tests provoke on
  // purpose (a 401 is how the app learns to sign in).
  consoleErrors: string[];
  web: string;
};

export const test = base.extend<Fixtures>({
  web: async ({ baseURL }, use) => use(baseURL as string),
  address: async ({}, use, info) => use(addressFor(info)),
  context: async ({ context, address }, use) => {
    await context.setExtraHTTPHeaders({ "X-Forwarded-For": address });
    await use(context);
  },
  consoleErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("console", (m) => {
        if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text());
      });
      page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
      await use(errors);
      expect(errors, "the browser console reported errors").toEqual([]);
    },
    { auto: true },
  ],
});

// ---- accounts, through the real API ------------------------------------------------------------------------------

export async function apiClient(playwright: typeof import("@playwright/test"), web: string, address: string,
                                token?: string): Promise<APIRequestContext> {
  return playwright.request.newContext({
    baseURL: web,
    extraHTTPHeaders: { "X-Forwarded-For": address, ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
}

export async function registerViaApi(api: APIRequestContext, account: Account): Promise<string> {
  const response = await api.post("/auth/register", { data: account });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id as string;
}

export async function apiLogin(api: APIRequestContext, account: Account): Promise<{ token: string; userId: string }> {
  const response = await api.post("/auth/login", { data: account });
  expect(response.status(), await response.text()).toBe(200);
  const body = await response.json();
  return { token: body.access_token as string, userId: body.user.id as string };
}

// ---- the real login form -----------------------------------------------------------------------------------------

// Fills in and submits the real form, then waits until the app shows the signed-in user: callers can navigate straight away
// without cancelling the sign-in request that is still in flight.
export async function signIn(page: Page, account: Account, next?: string): Promise<void> {
  await submitSignIn(page, account, next);
  await expectSignedIn(page, account);
}

export async function submitSignIn(page: Page, account: Account, next?: string): Promise<void> {
  await page.goto(next === undefined ? "/login" : `/login?next=${encodeURIComponent(next)}`);
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password").fill(account.password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

export async function expectSignedIn(page: Page, account: Account): Promise<void> {
  await expect(page.getByText(account.email, { exact: true })).toBeVisible();
}

export async function expectSignedOut(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

// A second, separate browser (its own cookie jar), as a different person or device would have.
export async function newSession(browser: Browser, web: string, address: string): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ baseURL: web, extraHTTPHeaders: { "X-Forwarded-For": address }, locale: "en-US" });
  return { context, page: await context.newPage() };
}

export async function refreshCookie(context: BrowserContext) {
  return (await context.cookies()).find((c) => c.name === "atlas_refresh");
}

// Everything the page itself could read or leak: storage, readable cookies and the address bar.
export async function readableState(page: Page): Promise<{ local: string; session: string; cookie: string; url: string }> {
  return page.evaluate(() => ({
    local: JSON.stringify({ ...window.localStorage }),
    session: JSON.stringify({ ...window.sessionStorage }),
    cookie: document.cookie,
    url: window.location.href,
  }));
}
