import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const empty = { items: [], next_cursor: null };
const review = {
  id: "review-1",
  type: "review_task",
  title: "Moderate physics paper",
  status: "in_review",
  version: 1,
  etag: 'W/"1"',
};

async function mockPortal(page) {
  await page.route("**/api/v2/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v2/me") {
      return route.fulfill({ json: { tenant_id: "school-a", roles: ["teacher"] } });
    }
    if (path === "/api/v2/events") {
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": ready\n\n" });
    }
    if (path === "/api/v2/reviews") return route.fulfill({ json: { ...empty, items: [review] } });
    return route.fulfill({ json: empty });
  });
}

test("teacher portal is keyboard accessible and has no serious axe findings", async ({ page }) => {
  await mockPortal(page);
  await page.goto("/portal");
  await expect(page.getByRole("heading", { name: /Evidence you can search/i })).toBeVisible();
  await page.getByRole("button", { name: "Reviews" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Moderate physics paper" })).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((item) => ["serious", "critical"].includes(item.impact || ""))).toEqual([]);
});

test("review decision uses the visible ETag and mobile layout does not overflow", async ({ page }) => {
  await mockPortal(page);
  let ifMatch = "";
  await page.route("**/api/v2/reviews/review-1/decision", async (route) => {
    ifMatch = route.request().headers()["if-match"] || "";
    return route.fulfill({ json: { ...review, status: "approved", version: 2, etag: 'W/"2"' } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/portal");
  await page.getByRole("button", { name: "Reviews" }).click();
  await page.getByRole("button", { name: "Approve" }).click();
  await page.getByLabel("Comment or rationale").fill("Checked against immutable evidence");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Review update saved and audited.")).toBeVisible();
  expect(ifMatch).toBe('W/"1"');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBe(false);
});

test("student revision dashboard exposes only approved practice and private progress", async ({ page }) => {
  await page.route("**/api/v2/student/revision-packs", (route) => route.fulfill({ json: { learner_id: "learner-1", next_cursor: null, items: [{ id: "pack-1", title: "Forces revision", status: "approved", payload: { introduction: "Review force diagrams.", items: [{ question_id: "q-1", text: "Explain resultant force.", teacher_approved: true }] } }] } }));
  await page.route("**/api/v2/revision-progress?**", (route) => route.fulfill({ json: { items: [{ id: "progress-1", title: "Forces", payload: { summary: "One question reviewed" } }], next_cursor: null } }));
  await page.goto("/revision");
  await expect(page.getByRole("heading", { name: "Forces revision" })).toBeVisible();
  await expect(page.getByText("Explain resultant force.")).toBeVisible();
  await expect(page.getByText("One question reviewed")).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((item) => ["serious", "critical"].includes(item.impact || ""))).toEqual([]);
});
