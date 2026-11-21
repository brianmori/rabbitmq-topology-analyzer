import { useState } from "react";
import type { AccessResponse, RunStatus, TopologyNode, TopologyNodeKind } from "../types/topology";

interface ExplorerSidebarProps {
  access: AccessResponse;
  vhosts: ReadonlyArray<string>;
  selectedVhosts: ReadonlySet<string>;
  visibleKinds: ReadonlySet<TopologyNodeKind>;
  hideDefaultExchanges: boolean;
  searchQuery: string;
  searchMatchCount: number;
  searchMatches: ReadonlyArray<TopologyNode>;
  searchFocusId: string | null;
  onSearchMatchSelect: (id: string) => void;
  sccStatusText: string;
  cycleStatusText: string;
  onShowComponents: () => void;
  onShowCycles: () => void;
  onReturnToGraph: () => void;
  sccRunStatus: RunStatus | null;
  sccCanCancel: boolean;
  sccCanRetry: boolean;
  cycleMaxCycles: string;
  cycleMaxCyclesError: string | null;
  cycleMaxCyclesUnbounded: boolean;
  cycleRunStatus: RunStatus | null;
  cycleCanCancel: boolean;
  cycleCanRetry: boolean;
  computeRunActive: boolean;
  onVhostChange: (vhost: string, visible: boolean) => void;
  onAllVhostsChange: (visible: boolean) => void;
  onKindChange: (kind: TopologyNodeKind, visible: boolean) => void;
  onHideDefaultExchangesChange: (hidden: boolean) => void;
  onSearchQueryChange: (query: string) => void;
  onStartScc: () => void;
  onCancelScc: () => void;
  onRetryScc: () => void;
  onCycleMaxCyclesChange: (value: string) => void;
  onStartCycle: () => void;
  onCancelCycle: () => void;
  onRetryCycle: () => void;
}

const KIND_LABELS: Readonly<Record<TopologyNodeKind, string>> = {
  exchange: "Exchanges ◇",
  queue: "Queues ▤",
  shovel: "Shovels ⇢",
};

export function ExplorerSidebar({
  access,
  vhosts,
  selectedVhosts,
  visibleKinds,
  hideDefaultExchanges,
  searchQuery,
  searchMatchCount,
  searchMatches,
  searchFocusId,
  onSearchMatchSelect,
  sccStatusText,
  cycleStatusText,
  onShowComponents,
  onShowCycles,
  onReturnToGraph,
  sccRunStatus,
  sccCanCancel,
  sccCanRetry,
  cycleMaxCycles,
  cycleMaxCyclesError,
  cycleMaxCyclesUnbounded,
  cycleRunStatus,
  cycleCanCancel,
  cycleCanRetry,
  computeRunActive,
  onVhostChange,
  onAllVhostsChange,
  onKindChange,
  onHideDefaultExchangesChange,
  onSearchQueryChange,
  onStartScc,
  onCancelScc,
  onRetryScc,
  onCycleMaxCyclesChange,
  onStartCycle,
  onCancelCycle,
  onRetryCycle,
}: ExplorerSidebarProps) {
  const [focusedMatchId, setFocusedMatchId] = useState<string | null>(null);
  const matchTabStop = searchMatches.some((node) => node.id === focusedMatchId)
    ? focusedMatchId
    : searchMatches[0]?.id;
  const sccActive =
    sccRunStatus === "queued" || sccRunStatus === "running" || sccRunStatus === "cancelling";
  const cycleActive =
    cycleRunStatus === "queued" || cycleRunStatus === "running" || cycleRunStatus === "cancelling";
  return (
    <aside className="operations-sidebar" aria-label="Topology exploration controls">
      <button type="button" className="text-button" onClick={onReturnToGraph}>
        Return to graph
      </button>
      <section className="sidebar-section">
        <h2>Find resources</h2>
        <label className="field-label" htmlFor="resource-search">
          Resource name
        </label>
        <input
          id="resource-search"
          type="search"
          value={searchQuery}
          onChange={(event) => onSearchQueryChange(event.target.value)}
          placeholder="e.g. orders"
          autoComplete="off"
        />
        <p className="field-hint" aria-live="polite">
          {searchQuery.trim()
            ? `${searchMatchCount} visible match${searchMatchCount === 1 ? "" : "es"}`
            : "Highlights and centers visible matches"}
        </p>
        {searchMatches.length > 0 && (
          <>
            <p id="resource-match-help" className="field-hint">
              Choose a match: ↑/↓ or Home/End, then Enter.
            </p>
            <div
              className="search-matches"
              role="group"
              aria-label="Visible resource matches"
              aria-describedby="resource-match-help"
              onKeyDown={(event) => {
                const buttons = [
                  ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
                ];
                const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
                const next =
                  event.key === "ArrowDown"
                    ? Math.min(index + 1, buttons.length - 1)
                    : event.key === "ArrowUp"
                      ? Math.max(index - 1, 0)
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? buttons.length - 1
                          : null;
                if (next === null) return;
                event.preventDefault();
                buttons[next]?.focus();
              }}
            >
              {searchMatches.map((node) => (
                <button
                  key={node.id}
                  type="button"
                  tabIndex={matchTabStop === node.id ? 0 : -1}
                  aria-pressed={searchFocusId === node.id}
                  onFocus={() => setFocusedMatchId(node.id)}
                  onClick={() => onSearchMatchSelect(node.id)}
                >
                  {node.data.isMissing ? "MISSING " : ""}
                  {node.data.kind} {node.data.name || "(default exchange)"}, vhost {node.data.vhost}
                </button>
              ))}
            </div>
          </>
        )}
      </section>

      <fieldset className="sidebar-section filter-group">
        <legend>Vhost scope</legend>
        <div className="inline-actions">
          <button type="button" className="text-button" onClick={() => onAllVhostsChange(true)}>
            Select all
          </button>
          <button type="button" className="text-button" onClick={() => onAllVhostsChange(false)}>
            Select none
          </button>
        </div>
        <div className="checkbox-list checkbox-list--scrollable">
          {vhosts.map((vhost) => (
            <label key={vhost}>
              <input
                type="checkbox"
                checked={selectedVhosts.has(vhost)}
                onChange={(event) => onVhostChange(vhost, event.target.checked)}
              />
              <span>{vhost}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="sidebar-section filter-group">
        <legend>Resource visibility</legend>
        <div className="checkbox-list">
          {Object.entries(KIND_LABELS).map(([kind, label]) => (
            <label key={kind}>
              <input
                type="checkbox"
                checked={visibleKinds.has(kind as TopologyNodeKind)}
                onChange={(event) => onKindChange(kind as TopologyNodeKind, event.target.checked)}
              />
              <span>{label}</span>
            </label>
          ))}
          <label>
            <input
              type="checkbox"
              checked={hideDefaultExchanges}
              onChange={(event) => onHideDefaultExchangesChange(event.target.checked)}
            />
            <span>Hide default exchanges</span>
          </label>
        </div>
      </fieldset>

      <section className="sidebar-section" aria-labelledby="operations-heading">
        <h2 id="operations-heading">Operations</h2>
        <p className="field-hint">
          Both operations analyze the entire capture. Exploration filters only change the drawing.
        </p>
        {access.capabilities.runGraphOperations ? (
          <div className="operation-controls">
            <button type="button" aria-disabled={computeRunActive} onClick={onStartScc}>
              {sccActive ? "Cyclic components running…" : "Run cyclic components"}
            </button>
            <button type="button" className="text-button" onClick={onShowComponents}>
              Open components
            </button>
            <p className="operation-status">{sccStatusText}</p>
            {sccActive && sccCanCancel && (
              <button
                type="button"
                className="secondary-button"
                disabled={sccRunStatus === "cancelling"}
                onClick={onCancelScc}
              >
                {sccRunStatus === "cancelling" ? "Cancelling…" : "Cancel analysis"}
              </button>
            )}
            {sccCanRetry && (
              <button type="button" className="secondary-button" onClick={onRetryScc}>
                Retry analysis recovery
              </button>
            )}
            <label className="field-label" htmlFor="max-cycles">
              Maximum single-vhost cycles
            </label>
            <input
              id="max-cycles"
              type="number"
              min="0"
              step="1"
              inputMode="numeric"
              value={cycleMaxCycles}
              disabled={computeRunActive}
              aria-describedby="max-cycles-hint"
              aria-invalid={cycleMaxCyclesError !== null}
              onChange={(event) => onCycleMaxCyclesChange(event.target.value)}
            />
            <p id="max-cycles-hint" className="field-hint">
              All cross-vhost cycles are included. Use zero to also include every single-vhost cycle.
            </p>
            {cycleMaxCyclesUnbounded && !cycleMaxCyclesError && (
              <p className="result-warning" role="alert">
                Zero enumerates all cycles and may require substantial time or memory. Keep this tab
                open so heartbeat polling continues.
              </p>
            )}
            {cycleMaxCyclesError && (
              <p className="field-error" role="alert">
                {cycleMaxCyclesError}
              </p>
            )}
            <button
              type="button"
              aria-disabled={computeRunActive || cycleMaxCyclesError !== null}
              onClick={onStartCycle}
            >
              {cycleActive ? "Structural cycles running…" : "Run structural cycles"}
            </button>
            <button type="button" className="text-button" onClick={onShowCycles}>
              Open cycles
            </button>
            <p className="operation-status">{cycleStatusText}</p>
            {cycleActive && cycleCanCancel && (
              <button
                type="button"
                className="secondary-button"
                disabled={cycleRunStatus === "cancelling"}
                onClick={onCancelCycle}
              >
                {cycleRunStatus === "cancelling" ? "Cancelling…" : "Cancel analysis"}
              </button>
            )}
            {cycleCanRetry && (
              <button type="button" className="secondary-button" onClick={onRetryCycle}>
                Retry analysis recovery
              </button>
            )}
          </div>
        ) : (
          <p className="field-hint">Your current access cannot run graph operations.</p>
        )}
      </section>
    </aside>
  );
}

export function GraphLegend({ focus = "explore" }: { focus?: "explore" | "components" | "cycle" }) {
  const focusClass =
    focus === "cycle"
      ? " topology-edge--cycle"
      : focus === "components"
        ? " topology-edge--scc scc-tone-0"
        : "";
  return (
    <section className="legend" aria-labelledby="legend-heading">
      <h2 id="legend-heading">Legend</h2>
      <ul>
        <li>
          <span className="legend-node legend-node--exchange">◇</span> Exchange
        </li>
        <li>
          <span className="legend-node legend-node--queue">▤</span> Queue
        </li>
        <li>
          <span className="legend-node legend-node--shovel">⇢</span> Shovel
        </li>
        <li>
          <span className="legend-node legend-node--missing">⚠</span> Referenced, not captured
        </li>
        {(
          [
            ["binding", "Binding"],
            ["dead_letter", "Dead letter"],
            ["alternate_exchange", "Alternate exchange"],
            ["shovel", "Shovel hop"],
          ] as const
        ).map(([kind, label]) => (
          <li key={kind}>
            <svg
              className="legend-edge"
              viewBox="0 0 36 12"
              aria-hidden="true"
              data-relationship={kind}
            >
              <path
                d="M 0 6 H 36"
                className={`topology-edge topology-edge--${kind}${focusClass}`}
              />
            </svg>{" "}
            {label}
          </li>
        ))}
      </ul>
    </section>
  );
}
