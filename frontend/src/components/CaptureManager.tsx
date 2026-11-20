import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, type CaptureUploadInput } from "../api/topology";
import type { AnalysisSummary, UploadResponse } from "../types/topology";

interface CaptureManagerProps {
  open: boolean;
  selectedAnalysis: AnalysisSummary | null;
  sessionAnalyses: ReadonlyArray<AnalysisSummary>;
  operationActive: boolean;
  onClose: () => void;
  onUpload: (input: CaptureUploadInput) => Promise<UploadResponse>;
  onDelete: (analysis: AnalysisSummary) => Promise<void>;
}

type MutationState =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "deleting" }
  | { kind: "error"; message: string }
  | { kind: "success"; message: string };

const ANALYSIS_ID_PATTERN = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/;
type UploadField = "analysis_id" | "definitions_file" | "queues_file" | "exchanges_file";
type FieldErrors = Partial<Record<UploadField, string>>;
const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function uploadErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "The capture could not be uploaded. Try again.";
  if (error.status === 413) {
    return "The selected capture files exceed the configured aggregate upload limit.";
  }
  if (error.code === "session_analysis_limit_reached") {
    return "This application session has reached its retained-capture limit. Delete a capture first.";
  }
  if (error.code === "session_store_capacity_reached") {
    return "The service is at its retained-capture capacity. Try again later.";
  }
  if (error.code === "analysis_capacity_unavailable") {
    const retry = error.retryAfter ? ` Retry after ${error.retryAfter} second(s).` : "";
    return `All analysis workers are busy.${retry}`;
  }
  if (error.code === "analysis_has_active_work") {
    return "That analysis has active upload or graph work. Stop it before replacing the capture.";
  }
  if (error.status === 400) {
    return "The SDK could not validate this RabbitMQ capture. Check that the files are matching exports.";
  }
  if (error.status === 422) {
    return "Check the analysis ID, label, host values, and optional queue/exchange file pair.";
  }
  return "The capture could not be uploaded safely. Try again.";
}

function deleteErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "The capture could not be deleted. Try again.";
  if (error.code === "analysis_has_active_work") {
    return "This capture has active upload or graph work. Stop it before deleting.";
  }
  if (error.status === 404) {
    return "This session capture no longer exists. The application catalogue was refreshed.";
  }
  return "The session capture could not be deleted safely. Try again.";
}

export function CaptureManager({
  open,
  selectedAnalysis,
  sessionAnalyses,
  operationActive,
  onClose,
  onUpload,
  onDelete,
}: CaptureManagerProps) {
  const [analysisId, setAnalysisId] = useState("");
  const [label, setLabel] = useState("");
  const [definitionsFile, setDefinitionsFile] = useState<File | null>(null);
  const [queuesFile, setQueuesFile] = useState<File | null>(null);
  const [exchangesFile, setExchangesFile] = useState<File | null>(null);
  const [hostText, setHostText] = useState("");
  const [mutation, setMutation] = useState<MutationState>({ kind: "idle" });
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const wasOpen = useRef(false);
  const submitGeneration = useRef(0);
  const dialogRef = useRef<HTMLElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) {
      wasOpen.current = false;
      return;
    }
    if (wasOpen.current) return;
    wasOpen.current = true;
    setAnalysisId(selectedAnalysis?.source === "session" ? selectedAnalysis.analysisId : "");
    setLabel(selectedAnalysis?.source === "session" ? selectedAnalysis.label : "");
    setDefinitionsFile(null);
    setQueuesFile(null);
    setExchangesFile(null);
    setHostText("");
    setMutation({ kind: "idle" });
    setFieldErrors({});
    submitGeneration.current += 1;
  }, [open, selectedAnalysis]);

  useEffect(() => {
    if (!open) return;
    openerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = requestAnimationFrame(() => {
      const dialog = dialogRef.current;
      if (dialog && !dialog.contains(document.activeElement)) {
        dialog.querySelector<HTMLElement>("[data-dialog-initial-focus]")?.focus();
      }
    });
    return () => {
      cancelAnimationFrame(frame);
      if (openerRef.current?.isConnected) openerRef.current.focus();
      openerRef.current = null;
    };
  }, [open]);

  if (!open) return null;
  const busy = mutation.kind === "uploading" || mutation.kind === "deleting";
  const selectedSession = selectedAnalysis?.source === "session" ? selectedAnalysis : null;
  const replacement = sessionAnalyses.find(
    (analysis) => analysis.source === "session" && analysis.analysisId === analysisId,
  );

  function clearFieldError(field: UploadField) {
    setFieldErrors((current) => {
      const next = { ...current };
      delete next[field];
      return next;
    });
  }

  return createPortal(
    <div className="capture-dialog-backdrop">
      <section
        ref={dialogRef}
        className="capture-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="capture-dialog-title"
        aria-describedby="capture-dialog-description"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            if (!busy) {
              event.preventDefault();
              onClose();
            }
            return;
          }
          if (event.key !== "Tab") return;
          const focusable = [
            ...(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? []),
          ];
          if (focusable.length === 0) {
            event.preventDefault();
            return;
          }
          const first = focusable[0];
          const last = focusable.at(-1);
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <div className="capture-dialog__heading">
          <div>
            <p className="eyebrow">Session workspace</p>
            <h2 id="capture-dialog-title">Manage captured analysis</h2>
          </div>
          <button
            type="button"
            className="text-button"
            data-dialog-initial-focus
            disabled={busy}
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <p id="capture-dialog-description" className="capture-dialog__notice">
          Captures are temporary and private to this application session. Its ownership identifier
          is never displayed.
        </p>

        <form
          className="capture-form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (busy || operationActive) return;
            const errors: FieldErrors = {};
            if (!ANALYSIS_ID_PATTERN.test(analysisId)) {
              errors.analysis_id =
                "Use 1–64 lowercase letters, digits, dots, underscores or hyphens. The first and last characters must be letters or digits.";
            }
            if (!definitionsFile) {
              errors.definitions_file = "Choose a definitions export.";
            }
            if ((queuesFile === null) !== (exchangesFile === null)) {
              errors[queuesFile === null ? "queues_file" : "exchanges_file"] =
                "Queue and exchange observation files must be supplied together.";
            }
            setFieldErrors(errors);
            const firstInvalidField = Object.keys(errors)[0] as UploadField | undefined;
            if (firstInvalidField || !definitionsFile) {
              setMutation({ kind: "idle" });
              event.currentTarget
                .querySelector<HTMLInputElement>(
                  `[name="${firstInvalidField ?? "definitions_file"}"]`,
                )
                ?.focus();
              return;
            }
            const inClusterAmqpHosts = hostText
              .split(/\r?\n|,/)
              .map((host) => host.trim())
              .filter(Boolean);
            const generation = submitGeneration.current + 1;
            submitGeneration.current = generation;
            setMutation({ kind: "uploading" });
            void onUpload({
              analysisId,
              label: label || undefined,
              definitionsFile,
              queuesFile: queuesFile ?? undefined,
              exchangesFile: exchangesFile ?? undefined,
              inClusterAmqpHosts,
            })
              .then((result) => {
                if (generation !== submitGeneration.current) return;
                setMutation({
                  kind: "success",
                  message: result.replaced
                    ? `Replaced ${result.analysis.label}.`
                    : `Uploaded ${result.analysis.label}.`,
                });
              })
              .catch((error: unknown) => {
                if (generation !== submitGeneration.current) return;
                setMutation({ kind: "error", message: uploadErrorMessage(error) });
              });
          }}
        >
          <div className="capture-form__grid">
            <div>
              <label>
                Analysis ID
                <input
                  name="analysis_id"
                  value={analysisId}
                  required
                  pattern="[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"
                  maxLength={64}
                  disabled={busy || operationActive}
                  aria-invalid={Boolean(fieldErrors.analysis_id)}
                  aria-describedby={`analysis-id-hint${fieldErrors.analysis_id ? " analysis-id-error" : ""}`}
                  onChange={(event) => {
                    setAnalysisId(event.target.value);
                    clearFieldError("analysis_id");
                  }}
                />
              </label>
              <p id="analysis-id-hint" className="field-hint">
                1–64 lowercase letters or digits; dots, underscores and hyphens are allowed inside.
              </p>
              {fieldErrors.analysis_id && (
                <p id="analysis-id-error" className="field-error" role="alert">
                  {fieldErrors.analysis_id}
                </p>
              )}
            </div>
            <label>
              Display label (optional)
              <input
                name="label"
                value={label}
                maxLength={120}
                disabled={busy || operationActive}
                onChange={(event) => setLabel(event.target.value)}
              />
            </label>
          </div>
          <div>
            <label>
              Definitions export
              <input
                name="definitions_file"
                type="file"
                accept="application/json,.json"
                required
                disabled={busy || operationActive}
                aria-invalid={Boolean(fieldErrors.definitions_file)}
                aria-describedby={
                  fieldErrors.definitions_file ? "definitions-file-error" : undefined
                }
                onChange={(event) => {
                  setDefinitionsFile(event.target.files?.[0] ?? null);
                  clearFieldError("definitions_file");
                }}
              />
            </label>
            {fieldErrors.definitions_file && (
              <p id="definitions-file-error" className="field-error" role="alert">
                {fieldErrors.definitions_file}
              </p>
            )}
          </div>
          <fieldset className="capture-form__observations" disabled={busy || operationActive}>
            <legend>Optional complete observation pair</legend>
            <div>
              <label>
                Queue observations
                <input
                  name="queues_file"
                  type="file"
                  accept="application/json,.json"
                  aria-invalid={Boolean(fieldErrors.queues_file)}
                  aria-describedby={fieldErrors.queues_file ? "queues-file-error" : undefined}
                  onChange={(event) => {
                    setQueuesFile(event.target.files?.[0] ?? null);
                    clearFieldError("queues_file");
                    clearFieldError("exchanges_file");
                  }}
                />
              </label>
              {fieldErrors.queues_file && (
                <p id="queues-file-error" className="field-error" role="alert">
                  {fieldErrors.queues_file}
                </p>
              )}
            </div>
            <div>
              <label>
                Exchange observations
                <input
                  name="exchanges_file"
                  type="file"
                  accept="application/json,.json"
                  aria-invalid={Boolean(fieldErrors.exchanges_file)}
                  aria-describedby={fieldErrors.exchanges_file ? "exchanges-file-error" : undefined}
                  onChange={(event) => {
                    setExchangesFile(event.target.files?.[0] ?? null);
                    clearFieldError("queues_file");
                    clearFieldError("exchanges_file");
                  }}
                />
              </label>
              {fieldErrors.exchanges_file && (
                <p id="exchanges-file-error" className="field-error" role="alert">
                  {fieldErrors.exchanges_file}
                </p>
              )}
            </div>
          </fieldset>
          <label>
            In-cluster AMQP hosts (optional)
            <textarea
              name="in_cluster_amqp_host"
              rows={3}
              value={hostText}
              disabled={busy || operationActive}
              placeholder="rabbitmq-0.rabbitmq&#10;rabbitmq.example.net"
              onChange={(event) => setHostText(event.target.value)}
            />
            <span>Enter one host per line or separate hosts with commas.</span>
          </label>
          <p className="capture-upload-intent" role="status">
            {replacement ? (
              <>
                <strong>
                  Replace session capture: {replacement.label} ({replacement.analysisId}).
                </strong>{" "}
                Changed effective inputs replace its topology and clear retained results. Identical
                inputs refresh expiry and keep retained results.
              </>
            ) : (
              "New session capture. Choose an existing session analysis ID to replace it."
            )}
          </p>
          <button type="submit" className="primary-button" disabled={busy || operationActive}>
            {mutation.kind === "uploading"
              ? "Uploading and processing…"
              : replacement
                ? "Replace capture"
                : "Upload capture"}
          </button>
        </form>

        {selectedSession && (
          <div className="capture-delete">
            <div>
              <strong>Delete selected session capture</strong>
              <span>{selectedSession.label}</span>
            </div>
            <button
              type="button"
              className="danger-button"
              disabled={busy || operationActive}
              onClick={() => {
                const generation = submitGeneration.current + 1;
                submitGeneration.current = generation;
                setMutation({ kind: "deleting" });
                void onDelete(selectedSession)
                  .then(() => {
                    if (generation !== submitGeneration.current) return;
                    setMutation({ kind: "success", message: "Session capture deleted." });
                    onClose();
                  })
                  .catch((error: unknown) => {
                    if (generation !== submitGeneration.current) return;
                    setMutation({ kind: "error", message: deleteErrorMessage(error) });
                  });
              }}
            >
              {mutation.kind === "deleting" ? "Deleting…" : "Delete capture"}
            </button>
          </div>
        )}

        {operationActive && (
          <p className="capture-dialog__status" role="status">
            Upload and deletion are paused while graph analysis is active.
          </p>
        )}
        {mutation.kind === "error" && (
          <p className="capture-dialog__error" role="alert">
            {mutation.message}
          </p>
        )}
        {mutation.kind === "success" && (
          <p className="capture-dialog__status" role="status">
            {mutation.message}
          </p>
        )}
        {(mutation.kind === "uploading" || mutation.kind === "deleting") && (
          <p className="capture-dialog__status" role="status">
            {mutation.kind === "uploading"
              ? "Receiving and validating the captured snapshot…"
              : "Removing the selected capture and retained results…"}
          </p>
        )}
      </section>
    </div>,
    document.body,
  );
}
