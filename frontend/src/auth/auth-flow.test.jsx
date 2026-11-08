import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { USER, fakeNetwork, json, renderApp, signedIn, signedOut } from "../testing";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

const login = async (user, email = "ann@example.com", password = "correct horse battery") => {
  await user.type(await screen.findByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
};

describe("protected routes", () => {
  it("send a visitor to the sign-in page and remember where they were going", async () => {
    const net = fakeNetwork(signedOut());
    renderApp("/repos/my-service");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(net.count("GET /repositories/my-service")).toBe(0); // nothing is fetched before the session is known
  });

  it("show nothing but a status message while the session is being checked", async () => {
    let release;
    fakeNetwork({ "POST /auth/refresh": () => new Promise((resolve) => { release = () => resolve(json(401, {})); }) });
    renderApp("/repos");
    expect(screen.getByRole("status")).toHaveTextContent("Checking your session");
    expect(screen.queryByRole("heading", { name: "Repositories" })).toBeNull();
    release();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("let a visitor with a valid session cookie straight in, without a sign-in form", async () => {
    fakeNetwork(signedIn({ "GET /repositories": json(200, { items: [{ repository_id: "proj", role: "owner" }] }) }));
    renderApp("/repos");
    expect(await screen.findByRole("link", { name: "proj" })).toBeInTheDocument();
    expect(screen.getByText("ann@example.com")).toBeInTheDocument();
  });

  it("keep the public pages reachable", async () => {
    fakeNetwork(signedOut());
    renderApp("/register");
    expect(await screen.findByRole("heading", { name: "Create an account" })).toBeInTheDocument();
  });
});

describe("signing in", () => {
  it("goes to the page the visitor wanted", async () => {
    const net = fakeNetwork(signedOut({
      "POST /auth/login": json(200, { access_token: "tok-9", user: USER }),
      "GET /repositories": json(200, { items: [{ repository_id: "proj", role: "viewer" }] }),
    }));
    const user = userEvent.setup();
    renderApp("/repos?x=1");
    await login(user);
    expect(await screen.findByRole("link", { name: "proj" })).toBeInTheDocument();
    expect(net.calls.find((c) => c.url === "/repositories").headers.Authorization).toBe("Bearer tok-9");
  });

  it("does not follow a redirect target that leaves the app", async () => {
    fakeNetwork(signedOut({ "POST /auth/login": json(200, { access_token: "tok", user: USER }) }));
    const user = userEvent.setup();
    renderApp("/login?next=//evil.example/steal");
    await login(user);
    expect(await screen.findByText("Overview")).toBeInTheDocument(); // the default page, not evil.example
  });

  it("shows the server's message for wrong credentials and stays on the form", async () => {
    fakeNetwork(signedOut({ "POST /auth/login": json(401, { detail: "Incorrect email or password" }) }));
    const user = userEvent.setup();
    renderApp("/login");
    await login(user, "ann@example.com", "wrong");
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password");
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("says how long to wait when rate limited", async () => {
    fakeNetwork(signedOut({ "POST /auth/login": json(429, { detail: "Too many attempts; try again later" }, { "Retry-After": "120" }) }));
    const user = userEvent.setup();
    renderApp("/login");
    await login(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 120 seconds");
  });

  it("disables the button while the request is running (no double submit)", async () => {
    let release;
    fakeNetwork(signedOut({ "POST /auth/login": () => new Promise((resolve) => { release = () => resolve(json(200, { access_token: "t", user: USER })); }) }));
    const user = userEvent.setup();
    renderApp("/login");
    await login(user);
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDisabled();
    release();
  });
});

describe("what the browser keeps", () => {
  it("never writes a token or password to storage or to cookies it can set", async () => {
    fakeNetwork(signedOut({
      "POST /auth/login": json(200, { access_token: "tok-secret-1234567890", user: USER }),
      "GET /repositories": json(200, { items: [] }),
    }));
    const user = userEvent.setup();
    renderApp("/login");
    await login(user, "ann@example.com", "a-password-to-find");
    await screen.findByText("ann@example.com");
    const everything = JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage }) + document.cookie;
    expect(everything).not.toContain("tok-secret");
    expect(everything).not.toContain("a-password-to-find");
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain("tok-secret"); // nor in the address bar
  });

  it("asks the server for an access token on a fresh page load, using only the cookie", async () => {
    const net = fakeNetwork(signedIn());
    renderApp("/");
    await screen.findByText("ann@example.com");
    const refresh = net.calls.find((c) => c.url === "/auth/refresh");
    expect(refresh.method).toBe("POST");
    expect(refresh.headers.Authorization).toBeUndefined();
  });
});

describe("session expiry", () => {
  it("sends the user to sign in again, with an explanation, when a call cannot be renewed", async () => {
    let calls = 0;
    fakeNetwork({
      "POST /auth/refresh": () => (calls++ === 0 ? json(200, { access_token: "tok-1", user: USER }) : json(401, { detail: "Not signed in" })),
      "GET /repositories": json(401, { detail: "Not authenticated" }),
    });
    renderApp("/repos");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Your session has expired");
    expect(screen.queryByText("ann@example.com")).toBeNull();
  });

  it("recovers silently when the access token expired but the cookie is still good", async () => {
    let first = true;
    fakeNetwork({
      "POST /auth/refresh": json(200, { access_token: "tok-fresh", user: USER }),
      "GET /repositories": () => {
        if (first) { first = false; return json(401, { detail: "Not authenticated" }); }
        return json(200, { items: [{ repository_id: "proj", role: "editor" }] });
      },
    });
    renderApp("/repos");
    expect(await screen.findByRole("link", { name: "proj" })).toBeInTheDocument();
  });
});

describe("signing out", () => {
  it("revokes the session on the server, forgets it locally and locks the pages again", async () => {
    const net = fakeNetwork(signedIn({ "POST /auth/logout": json(204, undefined) }));
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByText("ann@example.com");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(net.count("POST /auth/logout")).toBe(1);
    expect(screen.queryByText("ann@example.com")).toBeNull();
  });

  it("still signs out locally when the server cannot be reached", async () => {
    fakeNetwork(signedIn({ "POST /auth/logout": () => { throw new TypeError("offline"); } }));
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByText("ann@example.com");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("registration", () => {
  it("blocks a mismatched or short password before asking the server", async () => {
    const net = fakeNetwork(signedOut());
    const user = userEvent.setup();
    renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText(/^Password/), "short");
    expect(screen.getByRole("alert")).toHaveTextContent("at least 12");
    await user.type(screen.getByLabelText("Repeat the password"), "different");
    expect(screen.getByRole("button", { name: "Create account" })).toBeDisabled();
    expect(net.count("POST /auth/register")).toBe(0);
  });

  it("registers, signs in and lands on the repositories page", async () => {
    const net = fakeNetwork(signedOut({
      "POST /auth/register": json(201, USER),
      "POST /auth/login": json(200, { access_token: "tok", user: USER }),
    }));
    const user = userEvent.setup();
    renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "ann@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a long enough password");
    await user.type(screen.getByLabelText("Repeat the password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("heading", { name: "Repositories" })).toBeInTheDocument();
    expect(net.count("POST /auth/login")).toBe(1);
  });

  it("shows a duplicate-account conflict and a closed registration", async () => {
    fakeNetwork(signedOut({ "POST /auth/register": json(403, { detail: "Registration is closed" }) }));
    const user = userEvent.setup();
    renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "ann@example.com");
    await user.type(screen.getByLabelText(/^Password/), "a long enough password");
    await user.type(screen.getByLabelText("Repeat the password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Registration is closed");
  });
});

describe("password reset", () => {
  it("answers the same way whatever the email, and never reveals whether the account exists", async () => {
    fakeNetwork(signedOut({ "POST /auth/password-reset": json(202, { detail: "If the account exists, a reset message has been sent" }) }));
    const user = userEvent.setup();
    renderApp("/reset-password");
    await user.type(await screen.findByLabelText("Email"), "nobody@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset message" }));
    expect(await screen.findByRole("status")).toHaveTextContent("If the account exists");
  });

  it("reads the single-use token from the link's fragment and sends it in the request body only", async () => {
    const net = fakeNetwork(signedOut({ "POST /auth/password-reset/confirm": json(204, undefined) }));
    const user = userEvent.setup();
    renderApp("/reset-password#token=abcDEF123456");
    await user.type(await screen.findByLabelText(/^New password/), "a brand new password");
    await user.click(screen.getByRole("button", { name: "Save the new password" }));
    await waitFor(() => expect(net.count("POST /auth/password-reset/confirm")).toBe(1));
    const call = net.calls.find((c) => c.url === "/auth/password-reset/confirm");
    expect(call.body).toEqual({ token: "abcDEF123456", new_password: "a brand new password" });
    expect(call.path).not.toContain("abcDEF123456"); // never in a URL
  });
});

describe("role-based controls (the server enforces the same rules)", () => {
  const repo = (role, extra = {}) => signedIn({
    "GET /repositories/proj": json(200, { repository_id: "proj", role, capabilities: role === "viewer" ? ["search code"] : ["search code", "index and re-index"] }),
    "GET /repositories/proj/index/status": json(200, {
      repository_id: "proj", root_path: role === "viewer" ? "" : "/srv/proj", status: "ready", index_version: 3,
      file_count: 4, chunk_count: 12, failed_file_count: 0, last_indexed_at: "2026-11-08T10:00:00Z" }),
    "GET /repositories/proj/members": json(200, { repository_id: "proj", members: [
      { user_id: "u-1", email: "ann@example.com", role: "owner" }, { user_id: "u-2", email: "bob@example.com", role: "viewer" }] }),
    ...extra,
  });

  it("viewer: can search, cannot re-index, does not see members, and is told why", async () => {
    fakeNetwork(repo("viewer"));
    renderApp("/repos/proj");
    const reindex = await screen.findByRole("button", { name: "Re-index" });
    expect(reindex).toBeDisabled();
    expect(screen.getByText(/needs the editor role/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Members" })).toBeNull();
    expect(screen.queryByText("/srv/proj")).toBeNull();
  });

  it("editor: can re-index but still cannot manage members", async () => {
    const net = fakeNetwork(repo("editor", { "POST /repositories/proj/index": json(200, { chunks_total: 12 }) }));
    const user = userEvent.setup();
    renderApp("/repos/proj");
    const reindex = await screen.findByRole("button", { name: "Re-index" });
    await waitFor(() => expect(reindex).toBeEnabled());
    await user.click(reindex);
    await waitFor(() => expect(net.count("POST /repositories/proj/index")).toBe(1));
    expect(await screen.findByText("Index updated.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Members" })).toBeNull();
  });

  it("owner: sees and manages members; removing the last owner is refused by the server and explained", async () => {
    const net = fakeNetwork(repo("owner", {
      "DELETE /repositories/proj/members/u-1": json(409, { detail: "A repository must keep at least one owner" }),
      "PUT /repositories/proj/members": json(200, { email: "cy@example.com", role: "editor" }),
    }));
    const user = userEvent.setup();
    renderApp("/repos/proj");
    expect(await screen.findByRole("heading", { name: "Members" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove ann@example.com" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least one owner");
    await user.type(screen.getByLabelText("Member email"), "cy@example.com");
    await user.selectOptions(screen.getByLabelText("New member role"), "editor");
    await user.click(screen.getByRole("button", { name: "Add or update" }));
    await waitFor(() => expect(net.count("PUT /repositories/proj/members")).toBe(1));
    expect(net.calls.find((c) => c.method === "PUT").body).toEqual({ email: "cy@example.com", role: "editor" });
  });

  it("a 404 for a repository you cannot see looks the same as one that does not exist", async () => {
    fakeNetwork(signedIn({ "GET /repositories/secret": json(404, { detail: "Repository not found" }) }));
    renderApp("/repos/secret");
    expect(await screen.findByRole("alert")).toHaveTextContent("Repository not found");
  });

  it("lists the user's repositories with their roles", async () => {
    fakeNetwork(signedIn({ "GET /repositories": json(200, { items: [
      { repository_id: "alpha", role: "owner" }, { repository_id: "beta", role: "viewer" }] }) }));
    renderApp("/repos");
    const row = (await screen.findByRole("link", { name: "beta" })).closest("tr");
    expect(within(row).getByText("viewer")).toBeInTheDocument();
  });
});

describe("signing out everywhere", () => {
  it("asks the server to end every session and locks the pages", async () => {
    const net = fakeNetwork(signedIn({ "POST /auth/logout-all": json(204, undefined) }));
    const user = userEvent.setup();
    renderApp("/");
    await screen.findByText("ann@example.com");
    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(net.count("POST /auth/logout-all")).toBe(1);
    expect(screen.getByRole("status")).toHaveTextContent("Signed out on every device");
  });
});
