import { useEffect, useRef, useState } from "react";
import { ApiError, fetchTopologyExport } from "../api/topology";
import type { TopologyResponse } from "../types/topology";

interface TopologyExportButtonProps {
  snapshot: Pick<TopologyResponse, "source" | "analysisId" | "snapshotRevision">;
  isWorkspaceCurrent: () => boolean;
  onRefreshCapture: () => void;
}

type ExportError = "refresh" | "download";

export function TopologyExportButton({
  snapshot,
  isWorkspaceCurrent,
  onRefreshCapture,
}: TopologyExportButtonProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ExportError | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  async function download(): Promise<void> {
    if (request.current || !isWorkspaceCurrent()) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setError(null);
    try {
      const { blob, filename } = await fetchTopologyExport(snapshot, controller.signal);
      if (controller.signal.aborted || !isWorkspaceCurrent()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      try {
        document.body.append(link);
        link.click();
      } finally {
        link.remove();
        // Give the browser a turn to consume the download before releasing the file.
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      }
    } catch (cause: unknown) {
      if (controller.signal.aborted || !isWorkspaceCurrent()) return;
      setError(
        cause instanceof ApiError &&
          (cause.status === 404 || cause.code === "snapshot_revision_mismatch")
          ? "refresh"
          : "download",
      );
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="text-button capture-export-button"
        aria-disabled={pending}
        onClick={() => void download()}
      >
        {pending ? "Exporting topology…" : "Export topology as JSON"}
      </button>
      {error && (
        <div className="capture-export-error">
          <p role="alert">
            {error === "refresh"
              ? "This capture changed or expired. Refresh it before exporting."
              : "The topology could not be exported. Try again."}
          </p>
          {error === "refresh" && (
            <button type="button" className="text-button capture-export-button" onClick={onRefreshCapture}>
              Refresh capture
            </button>
          )}
        </div>
      )}
    </>
  );
}
