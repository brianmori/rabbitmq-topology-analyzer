import { expect, type Page } from "@playwright/test";

export async function completedLayoutGeneration(page: Page): Promise<string> {
  const canvas = page.locator(".topology-canvas");
  let generation = "";
  await expect.poll(async () => {
    // Read one DOM snapshot per attempt: a newer layout can supersede the
    // generation between separate Playwright calls, including while ready.
    const layout = await canvas.evaluate((element) => ({
      state: element.getAttribute("data-layout-state"),
      generation: element.getAttribute("data-layout-generation"),
      completedGeneration: element.getAttribute("data-layout-completed-generation"),
    }));
    generation = layout.generation ?? "";
    return {
      state: layout.state,
      completed: generation !== "" && generation !== "0"
        && layout.completedGeneration === generation,
    };
  }, { message: "Wait for the current layout generation and viewport framing to complete" })
    .toEqual({ state: "ready", completed: true });
  return generation;
}
