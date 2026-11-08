import { apiClient, apiLogin, expect, newAccount, registerViaApi, signIn, test } from "../support/fixtures";

// The evaluation dashboard against the real API: real configuration, a real reference run (the verifier checks fixed patches;
// no model is called and nothing costs money), real metrics. Nothing on the page is a mocked number.
test.describe("evaluation dashboard", () => {
  // The API allows ONE active evaluation at a time (a deliberate resource and budget limit), so the tests that start runs
  // must not overlap. The other spec files never start a run and keep running in parallel.
  test.describe.configure({ mode: "serial" });

  test("a new account sees honest empty states, and nothing that belongs to somebody else", async ({ page, playwright, web, address }) => {
    const account = newAccount("empty");
    await registerViaApi(await apiClient(playwright, web, address), account);
    await signIn(page, account);
    await expect(page.getByText("No evaluations yet")).toBeVisible();
    await page.getByRole("link", { name: "Start evaluation", exact: true }).click();
    await expect(page.getByText("No configuration exists yet")).toBeVisible();
    await page.getByRole("link", { name: "Compare", exact: true }).click();
    await expect(page.getByText("Two evaluations are needed to compare")).toBeVisible();
  });

  test("starting a reference evaluation: confirmation, live status, results with their denominators", async ({ page, playwright, web, address }) => {
    test.setTimeout(150_000);
    const account = newAccount("reference");
    const anonymous = await apiClient(playwright, web, address);
    await registerViaApi(anonymous, account);
    const { token } = await apiLogin(anonymous, account);
    const api = await apiClient(playwright, web, address, token);
    const name = `e2e-config-${Date.now().toString(36)}`;
    const created = await api.post("/evaluations/configurations", {
      data: { name, model: { provider: "anthropic", model_id: "claude-opus-5-5", effort: "high" },
              test_execution: { backend: "local", test_visibility: "blind" } } });
    expect(created.status(), await created.text()).toBe(201);

    await signIn(page, account);
    await page.getByRole("link", { name: "Start evaluation", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Start an evaluation" })).toBeVisible();
    await expect(page.getByLabel("Configuration")).toContainText(name);
    // the catalogue comes from the server, with its version
    await expect(page.getByText(/Benchmark cases \(patching-/)).toBeVisible();

    // Nothing starts by itself, and the paid mode needs an explicit budget and confirmation.
    await expect(page.getByRole("button", { name: "Start reference evaluation" })).toBeDisabled(); // no name yet
    await page.getByLabel("Name").fill("e2e reference run");
    await page.getByLabel("bug-003-pagination").check();
    await expect(page.getByText("No model calls and no cost.")).toBeVisible();
    await page.getByLabel(/Live — real model calls/).check();
    await expect(page.getByRole("button", { name: "Start paid evaluation" })).toBeDisabled();
    await expect(page.getByText(/Paid model calls, capped at/)).toBeVisible();
    await page.getByLabel(/Reference — fixed patches/).check();

    await page.getByRole("button", { name: "Start reference evaluation" }).click();
    await expect(page.getByRole("heading", { name: "e2e reference run" })).toBeVisible();
    expect(new URL(page.url()).pathname).toMatch(/^\/runs\/[0-9a-f-]{36}$/);

    // The page follows the run to the end without being reloaded.
    await expect(page.locator("h1 .badge")).toHaveText("completed", { timeout: 120_000 });
    await expect(page.getByText("reference · diagnosis recorded")).toBeVisible();
    await expect(page.getByRole("row", { name: /final verified rate/ })).toContainText(/\d+ \/ \d+/); // numerator / denominator shown
    await expect(page.getByRole("link", { name: "bug-003-pagination" }).first()).toBeVisible();
    await expect(page.getByText("n/a").first()).toBeVisible(); // unknown values are never shown as zero

    // Drill into a case: the evidence behind the verdict is on the page.
    await page.getByRole("link", { name: "bug-003-pagination" }).first().click();
    await expect(page.getByRole("heading", { name: "Why Atlas judged it this way" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Patch attempts" })).toBeVisible();

    // Overview lists it; the API agrees with what the page showed.
    await page.getByRole("link", { name: "Overview", exact: true }).click();
    await expect(page.getByRole("cell", { name: "e2e reference run" }).getByRole("link")).toBeVisible(); // in the table ...
    await expect(page.getByText("Total evaluations").locator("..")).toContainText("1"); // ... and counted in the summary
    const runs = await (await api.get("/evaluations")).json();
    expect(runs.page.total).toBe(1);
    expect(runs.items[0]).toMatchObject({ name: "e2e reference run", status: "completed", evaluation_mode: "reference" });
    expect(runs.items[0].total_cost_usd).toBeNull(); // no model, no cost recorded: not "$0"
  });

  test("somebody else cannot open the run, and a second run is not allowed to start while one is active", async ({ page, playwright, web, address }) => {
    const owner = newAccount("runowner");
    const other = newAccount("runother");
    const anonymous = await apiClient(playwright, web, address);
    await registerViaApi(anonymous, owner);
    await registerViaApi(anonymous, other);
    const ownerApi = await apiClient(playwright, web, address, (await apiLogin(anonymous, owner)).token);
    const otherApi = await apiClient(playwright, web, address, (await apiLogin(anonymous, other)).token);
    const config = await ownerApi.post("/evaluations/configurations", {
      data: { name: `cfg-${Date.now().toString(36)}`, model: { provider: "anthropic", model_id: "claude-opus-5-5" },
              test_execution: { backend: "local", test_visibility: "blind" } } });
    const configId = (await config.json()).id as string;
    const started = await ownerApi.post("/evaluations/run", { data: { name: "private run", configuration_id: configId, mode: "reference", benchmark_ids: ["bug-003-pagination"] } });
    expect(started.status(), await started.text()).toBe(202);
    const runId = (await started.json()).id as string;

    expect((await otherApi.get(`/evaluations/${runId}`)).status()).toBe(404); // not "forbidden": its existence is not revealed
    await signIn(page, other);
    await page.goto(`/runs/${runId}`);
    await expect(page.getByRole("alert")).toContainText("Evaluation not found");
    // a second request while the first is still running is refused with a clear 409
    const duplicate = await ownerApi.post("/evaluations/run", { data: { name: "again", configuration_id: configId, mode: "reference", benchmark_ids: ["bug-003-pagination"] } });
    expect([202, 409]).toContain(duplicate.status());
    if (duplicate.status() === 409) expect(JSON.stringify(await duplicate.json())).toMatch(/running|active|identical/i);
    await expect.poll(async () => (await (await ownerApi.get(`/evaluations/${runId}`)).json()).status, { timeout: 120_000 }).toBe("completed");
  });
});
