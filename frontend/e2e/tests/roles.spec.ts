import fs from "node:fs";
import type { APIRequestContext, Page } from "@playwright/test";
import { apiClient, apiLogin, expect, newAccount, newSession, registerViaApi, signIn, test, type Account } from "../support/fixtures";
import { readState } from "../support/state";

interface Person {
  account: Account;
  page: Page;
  api: APIRequestContext;
  userId: string;
}

const slug = () => `e2e-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

// Four real accounts, each in its own browser (own cookie jar) and each with an authenticated API client for the checks
// that go past what the UI offers: "the button is disabled" proves nothing unless the server refuses the same request.
async function team(browser: import("@playwright/test").Browser, playwright: typeof import("@playwright/test"), web: string, address: string) {
  const people: Record<"owner" | "editor" | "viewer" | "outsider", Person> = {} as never;
  for (const role of ["owner", "editor", "viewer", "outsider"] as const) {
    const account = newAccount(role);
    const anonymous = await apiClient(playwright, web, address);
    const userId = await registerViaApi(anonymous, account);
    const { token } = await apiLogin(anonymous, account);
    const { page } = await newSession(browser, web, address);
    await signIn(page, account);
    people[role] = { account, page, api: await apiClient(playwright, web, address, token), userId };
  }
  return people;
}

async function ownerCreatesRepository(owner: Person, id: string) {
  await owner.page.getByRole("link", { name: "Repositories", exact: true }).click();
  await expect(owner.page.getByText("You have no repositories yet")).toBeVisible();
  await owner.page.getByLabel("Identifier").fill(id);
  await owner.page.getByLabel(/Directory on the server/).fill(readState().fixture_repo);
  await owner.page.getByRole("button", { name: "Index repository" }).click();
  await expect(owner.page.getByRole("status")).toContainText(/Indexed \d+ code chunks/);
  await expect(owner.page.getByRole("link", { name: id })).toBeVisible();
}

async function addMember(owner: Person, id: string, member: Person, role: "viewer" | "editor" | "owner") {
  await owner.page.goto(`/repos/${id}`);
  await owner.page.getByLabel("Member email").fill(member.account.email);
  await owner.page.getByLabel("New member role").selectOption(role);
  await owner.page.getByRole("button", { name: "Add or update" }).click();
  await expect(owner.page.getByRole("row", { name: new RegExp(member.account.email) })).toBeVisible();
  await expect(owner.page.getByLabel(`Role of ${member.account.email}`)).toHaveValue(role);
}

test.describe("repository ownership and roles (four real accounts)", () => {
  test("the creator becomes the owner, and sees a role badge, the member list and the full controls", async ({ browser, playwright, web, address }) => {
    const { owner } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await expect(owner.page.getByRole("row", { name: new RegExp(`${id}.*owner`) })).toBeVisible();

    await owner.page.getByRole("link", { name: id }).click();
    await expect(owner.page.getByRole("heading", { name: new RegExp(id) })).toBeVisible();
    await expect(owner.page.getByRole("heading", { name: "Members" })).toBeVisible();
    await expect(owner.page.getByRole("button", { name: "Re-index" })).toBeEnabled();
    await owner.page.getByText("What your role allows").click();
    await expect(owner.page.getByText("manage members")).toBeVisible();
    await expect(owner.page.getByRole("row", { name: new RegExp(owner.account.email) })).toBeVisible();
  });

  test("viewer, editor and outsider each get exactly the controls their role allows", async ({ browser, playwright, web, address }) => {
    const { owner, editor, viewer, outsider } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await addMember(owner, id, editor, "editor");
    await addMember(owner, id, viewer, "viewer");

    // -- editor: can work on the repository, cannot administer it
    await editor.page.goto("/repos");
    await expect(editor.page.getByRole("row", { name: new RegExp(`${id}.*editor`) })).toBeVisible();
    await editor.page.getByRole("link", { name: id }).click();
    await expect(editor.page.getByRole("button", { name: "Re-index" })).toBeEnabled();
    await expect(editor.page.getByRole("heading", { name: "Members" })).toHaveCount(0);
    await editor.page.getByRole("button", { name: "Re-index" }).click();
    await expect(editor.page.getByText("Index updated.")).toBeVisible();

    // -- viewer: can read and search, is told why nothing else is available
    await viewer.page.goto(`/repos/${id}`);
    await expect(viewer.page.getByRole("row", { name: /viewer/ }).or(viewer.page.getByText("viewer").first())).toBeVisible();
    await expect(viewer.page.getByRole("button", { name: "Re-index" })).toBeDisabled();
    await expect(viewer.page.getByText(/needs the editor role/i)).toBeVisible();
    await expect(viewer.page.getByRole("heading", { name: "Members" })).toHaveCount(0);
    await viewer.page.getByLabel("Search query").fill("average");
    await viewer.page.getByRole("button", { name: "Search" }).click();
    await expect(viewer.page.getByText(/calculator\.py/).first()).toBeVisible();
    await expect(viewer.page.locator("main")).not.toContainText(readState().fixture_repo); // no server directory layout

    // -- outsider: the repository does not exist as far as they can tell
    await outsider.page.goto("/repos");
    await expect(outsider.page.getByText("You have no repositories yet")).toBeVisible();
    await outsider.page.goto(`/repos/${id}`);
    const forReal = await outsider.page.getByRole("alert").textContent();
    await outsider.page.goto(`/repos/${slug()}`);
    await expect(outsider.page.getByRole("alert")).toHaveText(forReal!); // identical to a repository that never existed
    expect(forReal).toContain("Repository not found");
  });

  test("the server refuses what the interface hides: every role, every sensitive endpoint", async ({ browser, playwright, web, address }) => {
    const { owner, editor, viewer, outsider } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await addMember(owner, id, editor, "editor");
    await addMember(owner, id, viewer, "viewer");
    const path = readState().fixture_repo;
    const call = (who: Person, method: "get" | "post" | "put" | "delete", url: string, data?: object) =>
      who.api[method](url, data ? { data } : undefined).then((r) => r.status());

    // reading
    for (const [who, expected] of [[owner, 200], [editor, 200], [viewer, 200], [outsider, 404]] as const) {
      expect(await call(who, "get", `/repositories/${id}`), `detail as ${who.account.email}`).toBe(expected);
      expect(await call(who, "get", `/repositories/${id}/index/status`)).toBe(expected);
      expect(await call(who, "post", `/repositories/${id}/search`, { query: "average" })).toBe(expected);
    }
    // changing the index
    expect(await call(viewer, "post", `/repositories/${id}/index`, { repository_path: path })).toBe(403);
    expect(await call(outsider, "post", `/repositories/${id}/index`, { repository_path: path })).toBe(404);
    expect(await call(editor, "post", `/repositories/${id}/index`, { repository_path: path })).toBe(200);
    // starting a model-backed investigation or generating patches
    expect(await call(viewer, "post", "/debug/investigate", { repository_id: id, bug_report: "average crashes on an empty list" })).toBe(403);
    expect(await call(outsider, "post", "/debug/investigate", { repository_id: id, bug_report: "average crashes on an empty list" })).toBe(404);
    // administration
    for (const who of [editor, viewer]) {
      expect(await call(who, "get", `/repositories/${id}/members`)).toBe(403);
      expect(await call(who, "put", `/repositories/${id}/members`, { email: who.account.email, role: "owner" })).toBe(403); // no self-promotion
      expect(await call(who, "delete", `/repositories/${id}/index`)).toBe(403);
      expect(await call(who, "delete", `/repositories/${id}/graph`)).toBe(403);
    }
    expect(await call(outsider, "get", `/repositories/${id}/members`)).toBe(404);
    expect(await call(outsider, "put", `/repositories/${id}/members`, { email: outsider.account.email, role: "owner" })).toBe(404);
    // the outsider cannot take the repository over by indexing it under the same name from their own path
    expect(await call(outsider, "post", `/repositories/${id}/index`, { repository_path: path })).toBe(404);
    // the viewer's view of the index hides the server's directory; the editor's and owner's do not
    expect((await (await viewer.api.get(`/repositories/${id}/index/status`)).json()).root_path).toBe("");
    expect((await (await owner.api.get(`/repositories/${id}/index/status`)).json()).root_path).toBe(fs.realpathSync(path)); // the server stores the resolved path (macOS: /var is /private/var)
    // and the owner can administer
    expect(await call(owner, "get", `/repositories/${id}/members`)).toBe(200);
  });

  test("role changes take effect without signing in again", async ({ browser, playwright, web, address }) => {
    const { owner, viewer } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await addMember(owner, id, viewer, "viewer");
    await viewer.page.goto(`/repos/${id}`);
    await expect(viewer.page.getByRole("button", { name: "Re-index" })).toBeDisabled();

    await owner.page.getByLabel(`Role of ${viewer.account.email}`).selectOption("editor"); // promote
    await expect(owner.page.getByLabel(`Role of ${viewer.account.email}`)).toHaveValue("editor");
    await viewer.page.reload();
    await expect(viewer.page.getByRole("button", { name: "Re-index" })).toBeEnabled();

    await owner.page.getByRole("button", { name: `Remove ${viewer.account.email}` }).click(); // remove
    await expect(owner.page.getByRole("row", { name: new RegExp(viewer.account.email) })).toHaveCount(0);
    await viewer.page.reload();
    await expect(viewer.page.getByRole("alert")).toContainText("Repository not found"); // access gone, not "forbidden"
    await viewer.page.goto("/repos");
    await expect(viewer.page.getByText("You have no repositories yet")).toBeVisible();
  });

  test("a repository always keeps an owner, and a second owner can administer", async ({ browser, playwright, web, address }) => {
    const { owner, editor } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await owner.page.goto(`/repos/${id}`);

    await owner.page.getByRole("button", { name: `Remove ${owner.account.email}` }).click(); // the only owner removes themselves
    await expect(owner.page.getByRole("alert")).toContainText("at least one owner");
    await owner.page.getByLabel(`Role of ${owner.account.email}`).selectOption("viewer"); // or demotes themselves
    await expect(owner.page.getByRole("alert")).toContainText("at least one owner");
    await expect(owner.page.getByLabel(`Role of ${owner.account.email}`)).toHaveValue("owner");

    await addMember(owner, id, editor, "owner");
    await editor.page.goto(`/repos/${id}`);
    await expect(editor.page.getByRole("heading", { name: "Members" })).toBeVisible(); // the new owner can administer
    await owner.page.goto(`/repos/${id}`);
    await owner.page.getByRole("button", { name: `Remove ${owner.account.email}` }).click(); // now the first owner may leave
    await owner.page.reload();
    await expect(owner.page.getByRole("alert")).toContainText("Repository not found");
  });

  test("indexing an identifier someone else owns is refused: nobody can take over or probe a repository", async ({ browser, playwright, web, address }) => {
    const { owner, outsider } = await team(browser, playwright, web, address);
    const id = slug();
    await ownerCreatesRepository(owner, id);
    await outsider.page.goto("/repos");
    await outsider.page.getByLabel("Identifier").fill(id);
    await outsider.page.getByLabel(/Directory on the server/).fill(readState().fixture_repo);
    await outsider.page.getByRole("button", { name: "Index repository" }).click();
    await expect(outsider.page.getByRole("alert")).toContainText("does not exist, or you do not have access");
    await expect(outsider.page.getByRole("link", { name: id })).toHaveCount(0);
    // a path outside the allowed roots never creates a repository, whoever asks
    await outsider.page.getByLabel("Identifier").fill(slug());
    await outsider.page.getByLabel(/Directory on the server/).fill("/etc");
    await outsider.page.getByRole("button", { name: "Index repository" }).click();
    await expect(outsider.page.getByRole("alert")).toContainText("Not allowed");
  });

  test("evaluation data is private to the account that created it", async ({ browser, playwright, web, address }) => {
    const { owner, outsider } = await team(browser, playwright, web, address);
    const config = {
      name: `private-${slug()}`,
      model: { provider: "anthropic", model_id: "claude-opus-5-5", effort: "high" },
      test_execution: { backend: "local", test_visibility: "blind" },
    };
    const created = await owner.api.post("/evaluations/configurations", { data: config });
    expect(created.status(), await created.text()).toBe(201);
    const { id } = await created.json();
    expect((await owner.api.get(`/evaluations/configurations/${id}`)).status()).toBe(200);
    expect((await outsider.api.get(`/evaluations/configurations/${id}`)).status()).toBe(404);
    expect((await outsider.api.post(`/evaluations/configurations/${id}/clone`, { data: {} })).status()).toBe(404);
    const listed = await (await outsider.api.get("/evaluations/configurations")).json();
    expect(listed.items.map((c: { id: string }) => c.id)).not.toContain(id);
    await outsider.page.goto("/start"); // and the page offers them nothing of the owner's
    await expect(outsider.page.getByText(config.name)).toHaveCount(0);
  });
});
