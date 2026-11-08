import { apiClient, expect, newAccount, registerViaApi, signIn, test } from "../support/fixtures";

const SIZES = [
  { name: "phone", width: 375, height: 812 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
];

async function noHorizontalScroll(page: import("@playwright/test").Page) {
  const { overflow, offenders } = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const wide = [...document.querySelectorAll("body *")]
      .filter((el) => el.getBoundingClientRect().right > width + 1 && !el.closest(".scroll"))
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${el.className || ""} "${(el.textContent || "").trim().slice(0, 30)}" right=${Math.round(el.getBoundingClientRect().right)}`);
    return { overflow: document.documentElement.scrollWidth - width, offenders: wide };
  });
  expect(overflow, `${page.url()} scrolls sideways; wide elements: ${offenders.join(" | ")}`).toBeLessThanOrEqual(0);
}

for (const size of SIZES) {
  test.describe(`${size.name} (${size.width}px)`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the public pages fit the screen and can be used", async ({ page }) => {
      for (const path of ["/login", "/register", "/reset-password"]) {
        await page.goto(path);
        await expect(page.getByRole("heading").first()).toBeVisible();
        await noHorizontalScroll(page);
      }
      await page.goto("/login");
      const button = page.getByRole("button", { name: "Sign in" });
      const box = (await button.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(size.width);
      expect(box.height).toBeGreaterThanOrEqual(30);
    });

    test("the signed-in pages fit the screen: tables scroll inside their own box, not the page", async ({ page, playwright, web, address }) => {
      const account = newAccount(`size${size.name}`);
      await registerViaApi(await apiClient(playwright, web, address), account);
      await signIn(page, account);
      for (const path of ["/", "/repos", "/start", "/compare"]) {
        await page.goto(path);
        await expect(page.getByRole("heading").first()).toBeVisible();
        await noHorizontalScroll(page);
      }
      // every control in the header is reachable
      await page.goto("/");
      for (const name of ["Overview", "Repositories", "Start evaluation", "Compare"]) {
        await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
      }
      await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
      await page.screenshot({ path: `test-results/responsive-${size.name}.png`, fullPage: true });
    });
  });
}
