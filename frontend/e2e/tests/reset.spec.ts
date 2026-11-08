import { expireResetTokens, resetTokenHashes } from "../support/db";
import { apiClient, apiLogin, expect, expectSignedIn, expectSignedOut, newAccount, newSession, PASSWORD, readableState, registerViaApi, signIn, submitSignIn, test } from "../support/fixtures";
import { latestResetToken, resetMessagesFor } from "../support/mail";

const NEW_PASSWORD = "a completely different passphrase";

async function requestReset(page: import("@playwright/test").Page, email: string) {
  await page.goto("/reset-password");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Send reset message" }).click();
  await expect(page.getByRole("status")).toContainText("If the account exists");
}

test.describe("password reset (real frontend, real API)", () => {
  test("the page answers identically for a known and an unknown address, and only a real account gets a message", async ({ page, playwright, web, address }) => {
    const known = newAccount("known");
    const unknown = newAccount("unknown");
    await registerViaApi(await apiClient(playwright, web, address), known);

    await requestReset(page, unknown.email);
    const forUnknown = await page.getByRole("status").textContent();
    await requestReset(page, known.email);
    expect(await page.getByRole("status").textContent()).toBe(forUnknown);

    expect(await resetMessagesFor(known.email, 1)).toBe(1);
    expect(await resetMessagesFor(unknown.email)).toBe(0); // waits a moment: nothing may arrive
    expect(await resetMessagesFor(known.email)).toBe(1);
    expect(await resetTokenHashes(unknown.email)).toEqual([]);
  });

  test("the whole journey: request, follow the link, choose a password, sign in with it", async ({ page, playwright, web, address }) => {
    const account = newAccount("journey");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await requestReset(page, account.email);
    const token = await latestResetToken(account.email);
    expect(token.length).toBeGreaterThanOrEqual(40);
    // Only a hash of it is stored; the token itself is not in the database.
    const stored = await resetTokenHashes(account.email);
    expect(stored).toHaveLength(1);
    expect(stored[0]).not.toContain(token);

    const urls: string[] = [];
    page.on("request", (r) => urls.push(r.url()));
    await page.goto(`/reset-password#token=${token}`);
    await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
    // The link's secret is removed from the address bar and was never part of a request URL (a fragment is not sent).
    expect((await readableState(page)).url).not.toContain(token);
    expect(urls.filter((u) => u.includes(token))).toEqual([]);

    await page.getByLabel(/^New password/).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expectSignedOut(page);

    await submitSignIn(page, account); // the old password no longer works ...
    await expect(page.getByRole("alert")).toContainText("Incorrect email or password");
    await signIn(page, { ...account, password: NEW_PASSWORD }); // ... the new one does
    await expect(page.getByRole("heading", { name: "Evaluations" })).toBeVisible();
  });

  test("resetting the password ends every session that already exists, immediately", async ({ browser, page, playwright, web, address }) => {
    const account = newAccount("sessions");
    await registerViaApi(await apiClient(playwright, web, address), account);
    const device = await newSession(browser, web, address);
    await signIn(device.page, account); // an existing session on another device
    await device.page.getByRole("link", { name: "Repositories", exact: true }).click();
    await expect(device.page.getByRole("heading", { name: "Repositories" })).toBeVisible();

    await page.waitForTimeout(1200);
    await requestReset(page, account.email);
    await page.goto(`/reset-password#token=${await latestResetToken(account.email)}`);
    await page.getByLabel(/^New password/).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expectSignedOut(page);

    // The other device has an unexpired access token and a refresh cookie, and neither works any more.
    await device.page.getByRole("link", { name: "Overview", exact: true }).click();
    await expectSignedOut(device.page);
    await expect(device.page.getByRole("status")).toContainText("Your session has expired");
    await device.context.close();
  });

  test("a reset link works once", async ({ page, playwright, web, address }) => {
    const account = newAccount("once");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await requestReset(page, account.email);
    const token = await latestResetToken(account.email);
    await page.goto(`/reset-password#token=${token}`);
    await page.getByLabel(/^New password/).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expectSignedOut(page);

    await page.goto(`/reset-password#token=${token}`); // the same link again
    await page.getByLabel(/^New password/).fill(NEW_PASSWORD + " again");
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expect(page.getByRole("alert")).toContainText("invalid or has expired");
    const api = await apiClient(playwright, web, address);
    await apiLogin(api, { ...account, password: NEW_PASSWORD }); // the first use really did set the password (fails if not 200)
  });

  test("an expired link and a made-up link are refused with the same message", async ({ page, playwright, web, address }) => {
    const account = newAccount("expired");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await requestReset(page, account.email);
    const token = await latestResetToken(account.email);
    await expireResetTokens(account.email);

    const messages: string[] = [];
    for (const candidate of [token, "x".repeat(43)]) {
      await page.goto(`/reset-password#token=${candidate}`);
      await page.getByLabel(/^New password/).fill(NEW_PASSWORD);
      await page.getByRole("button", { name: "Save the new password" }).click();
      messages.push((await page.getByRole("alert").textContent()) ?? "");
    }
    expect(messages[0]).toContain("invalid or has expired");
    expect(messages[1]).toBe(messages[0]);
  });

  test("the server's password policy applies to a reset too", async ({ page, playwright, web, address }) => {
    const account = newAccount("policy");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await requestReset(page, account.email);
    await page.goto(`/reset-password#token=${await latestResetToken(account.email)}`);
    await page.getByLabel(/^New password/).fill("aaaaaaaaaaaaaaaa"); // long enough for the browser, too guessable for the server
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expect(page.getByRole("alert")).toContainText("too easy to guess");
    await page.getByLabel(/^New password/).fill(NEW_PASSWORD); // the link is still usable after a refused attempt
    await page.getByRole("button", { name: "Save the new password" }).click();
    await expectSignedOut(page);
  });

  test("requests for resets are rate limited", async ({ page, playwright, web, address }) => {
    const account = newAccount("flood");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await page.goto("/reset-password");
    await page.getByLabel("Email").fill(account.email);
    for (let i = 0; i < 5; i++) {
      await page.getByRole("button", { name: "Send reset message" }).click();
      await expect(page.getByRole("status")).toContainText("If the account exists");
    }
    await page.getByRole("button", { name: "Send reset message" }).click();
    await expect(page.getByRole("alert")).toContainText("Too many attempts");
    expect(await resetMessagesFor(account.email, 5)).toBe(5);
    expect(await resetMessagesFor(account.email)).toBe(5); // and the flood did not become a flood of messages
  });
});
