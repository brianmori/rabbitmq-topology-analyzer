import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/topology";
import { catalogueFixture } from "../test/api-fixtures";
import type { UploadResponse } from "../types/topology";
import { CaptureManager } from "./CaptureManager";

const uploadResponse = {
  analysis: {
    ...catalogueFixture.static[0],
    source: "session",
    analysisId: "uploaded",
    label: "Uploaded",
    expiresAt: "2026-08-11T20:00:00Z",
  },
  replaced: false,
} as const satisfies UploadResponse;

async function prepareUpload(user: ReturnType<typeof userEvent.setup>) {
  const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus());
  const analysisId = within(dialog).getByLabelText("Analysis ID");
  await user.type(analysisId, "uploaded");
  expect(analysisId).toHaveValue("uploaded");
  await user.upload(
    within(dialog).getByLabelText("Definitions export"),
    new File(["definitions"], "definitions.json", { type: "application/json" }),
  );
  return dialog;
}

describe("CaptureManager", () => {
  it.each([
    ["new-capture", "Upload capture", null],
    ["uploaded", "Replace capture", "Uploaded (uploaded)"],
    ["another-session", "Replace capture", "Another session (another-session)"],
    [catalogueFixture.static[0].analysisId, "Upload capture", null],
  ])(
    "identifies the action for typed analysis ID %s from the caller's session catalogue",
    async (id, action, target) => {
      const user = userEvent.setup();
      const other = {
        ...uploadResponse.analysis,
        analysisId: "another-session",
        label: "Another session",
      };
      render(
        <CaptureManager
          open
          selectedAnalysis={uploadResponse.analysis}
          sessionAnalyses={[uploadResponse.analysis, other]}
          operationActive={false}
          onClose={vi.fn()}
          onUpload={vi.fn()}
          onDelete={vi.fn()}
        />,
      );
      await waitFor(() => expect(screen.getByRole("button", { name: "Close" })).toHaveFocus());
      const input = screen.getByLabelText("Analysis ID");
      await user.clear(input);
      await user.type(input, id);
      expect(screen.getByRole("button", { name: action })).toBeEnabled();
      if (target) {
        expect(screen.getByText(`Replace session capture: ${target}.`)).toBeVisible();
        expect(
          screen.getByText(
            /Changed effective inputs replace its topology and clear retained results/,
          ),
        ).toBeVisible();
        expect(
          screen.getByText(/Identical inputs refresh expiry and keep retained results/),
        ).toBeVisible();
      } else {
        expect(screen.getByText(/New session capture/)).toBeVisible();
        expect(screen.queryByRole("button", { name: "Replace capture" })).not.toBeInTheDocument();
      }
    },
  );

  it("associates an invalid trailing character with the ID, focuses it and retains files for correction", async () => {
    const user = userEvent.setup();
    const upload = vi.fn().mockResolvedValue(uploadResponse);
    render(
      <CaptureManager
        open
        selectedAnalysis={null}
        sessionAnalyses={[]}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    const dialog = await prepareUpload(user);
    const input = within(dialog).getByLabelText("Analysis ID");
    const definitions = within(dialog).getByLabelText<HTMLInputElement>("Definitions export");
    const file = definitions.files![0];
    await user.clear(input);
    await user.type(input, "ux-example-");
    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(
      /first and last characters must be letters or digits/,
    );
    expect(definitions.files![0]).toBe(file);
    expect(upload).not.toHaveBeenCalled();
    await user.clear(input);
    await user.type(input, "ux-example");
    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));
    expect(upload).toHaveBeenCalledWith(
      expect.objectContaining({ analysisId: "ux-example", definitionsFile: file }),
    );
  });

  it("associates a missing definitions error with the file input and focuses it", async () => {
    const user = userEvent.setup();
    const upload = vi.fn();
    render(
      <CaptureManager
        open
        selectedAnalysis={null}
        sessionAnalyses={[]}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Close" })).toHaveFocus());
    await user.type(screen.getByLabelText("Analysis ID"), "ux-example");
    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const input = screen.getByLabelText("Definitions export");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Choose a definitions export.");
    expect(upload).not.toHaveBeenCalled();
  });
  it("keeps the analysis ID focused if the user reaches it before initial focus runs", async () => {
    const user = userEvent.setup();
    const requestFrame = vi.spyOn(window, "requestAnimationFrame").mockReturnValueOnce(0);
    render(
      <CaptureManager
        open
        selectedAnalysis={null}
        sessionAnalyses={[]}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("Analysis ID");
    await user.click(input);
    const [[initialFocus]] = requestFrame.mock.calls;
    initialFocus(0);
    await user.keyboard("session-capture");

    expect(input).toHaveFocus();
    expect(input).toHaveValue("session-capture");
  });

  it("traps keyboard focus, closes with Escape, and restores the opener", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const manager = (open: boolean) => (
      <>
        <button type="button">Open capture manager</button>
        <CaptureManager
          open={open}
          sessionAnalyses={[]}
          selectedAnalysis={null}
          operationActive={false}
          onClose={onClose}
          onUpload={vi.fn()}
          onDelete={vi.fn()}
        />
      </>
    );
    const view = render(manager(false));
    const opener = screen.getByRole("button", { name: "Open capture manager" });
    opener.focus();

    view.rerender(manager(true));
    const close = await screen.findByRole("button", { name: "Close" });
    await waitFor(() => expect(close).toHaveFocus());

    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Upload capture" })).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    view.rerender(manager(false));
    expect(opener).toHaveFocus();
  });

  it("shows an indeterminate receipt/processing state until upload completes", async () => {
    const user = userEvent.setup();
    let resolveUpload: ((response: UploadResponse) => void) | undefined;
    const upload = vi.fn(
      () =>
        new Promise<UploadResponse>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const onClose = vi.fn();
    const { rerender } = render(
      <CaptureManager
        open
        sessionAnalyses={[]}
        selectedAnalysis={null}
        operationActive={false}
        onClose={onClose}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    const dialog = await prepareUpload(user);

    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));

    expect(
      within(dialog).getByRole("button", { name: "Uploading and processing…" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByText("Receiving and validating the captured snapshot…"),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    rerender(
      <CaptureManager
        open
        sessionAnalyses={[]}
        selectedAnalysis={uploadResponse.analysis}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    expect(
      within(dialog).getByRole("button", { name: "Uploading and processing…" }),
    ).toBeDisabled();
    resolveUpload?.(uploadResponse);
    expect(await within(dialog).findByText("Uploaded Uploaded.")).toBeVisible();
  });

  it.each([
    ["Queue observations", "Exchange observations"],
    ["Exchange observations", "Queue observations"],
  ])("focuses missing %s after choosing only %s", async (missing, supplied) => {
    const user = userEvent.setup();
    const upload = vi.fn().mockResolvedValue(uploadResponse);
    render(
      <CaptureManager
        open
        sessionAnalyses={[]}
        selectedAnalysis={null}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    const dialog = await prepareUpload(user);
    await user.upload(
      within(dialog).getByLabelText(supplied),
      new File(["queues"], "queues.json", { type: "application/json" }),
    );

    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));

    expect(upload).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Queue and exchange observation files must be supplied together.",
    );
    expect(within(dialog).getByLabelText(missing)).toHaveFocus();
    expect(within(dialog).getByLabelText(missing)).toHaveAttribute("aria-invalid", "true");
    expect(within(dialog).getByLabelText(missing)).toHaveAccessibleDescription(
      "Queue and exchange observation files must be supplied together.",
    );
  });

  it.each([
    [new ApiError(413, "capture_upload_too_large", "", null), "exceed the configured aggregate"],
    [new ApiError(400, "invalid_capture", "", null), "SDK could not validate"],
    [
      new ApiError(409, "session_analysis_limit_reached", "", null),
      "reached its retained-capture limit",
    ],
    [
      new ApiError(503, "session_store_capacity_reached", "", "1"),
      "service is at its retained-capture capacity",
    ],
    [new ApiError(409, "analysis_has_active_work", "", null), "has active upload or graph work"],
    [new ApiError(503, "analysis_capacity_unavailable", "", "2"), "Retry after 2 second(s)"],
  ])("renders an actionable safe upload error for %#", async (error, expected) => {
    const user = userEvent.setup();
    const upload = vi.fn().mockRejectedValue(error);
    render(
      <CaptureManager
        open
        sessionAnalyses={[]}
        selectedAnalysis={null}
        operationActive={false}
        onClose={vi.fn()}
        onUpload={upload}
        onDelete={vi.fn()}
      />,
    );
    const dialog = await prepareUpload(user);

    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(expected);
    expect(upload).toHaveBeenCalledOnce();
    expect(upload).toHaveBeenCalledWith(expect.objectContaining({ analysisId: "uploaded" }));
  });
});
