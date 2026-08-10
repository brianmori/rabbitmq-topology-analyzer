/**
 * Every fetch() call in this app lives here, nowhere else -- components
 * import from this module rather than calling fetch() directly.
 *
 * All requests go through the relative `/api/...` path. During local dev,
 * Vite's proxy (see vite.config.ts) forwards these to the FastAPI backend
 * at http://localhost:8000, so this code never needs to know the backend's
 * real address and never triggers a CORS preflight.
 */

import type {
  AccessResponse,
  AnalysisCatalogueResponse,
  UploadResponse,
} from '../types/topology'

/**
 * Returns backend-computed capabilities for the current principal.
 *
 * Components consume these booleans instead of interpreting a future JWT or
 * duplicating the backend's analyst-to-viewer role hierarchy.
 */
export async function fetchAccess(): Promise<AccessResponse> {
  const response = await fetch('/api/access')
  if (!response.ok) {
    throw new Error(`GET /api/access failed: ${response.status}`)
  }
  return response.json() as Promise<AccessResponse>
}

/**
 * Reads the session token from sessionStorage, generating and storing one
 * if this is the first call this browser session has made.
 *
 * A session token identifies this browser tab's ephemeral uploads to the
 * backend. It's machine-generated and never shown to the user -- the
 * human-readable name they choose is the separate `analysisId`.
 */
function getSessionToken(): string {
  const key = 'rabbitmq-topology-session-token'
  let token = sessionStorage.getItem(key)
  if (!token) {
    token = crypto.randomUUID()
    sessionStorage.setItem(key, token)
  }
  return token
}

/**
 * Fetches the list of static and session analyses available to this
 * browser session.
 */
export async function fetchAnalyses(): Promise<AnalysisCatalogueResponse> {
  const response = await fetch('/api/analyses', {
    headers: { 'X-Session-Token': getSessionToken() },
  })
  if (!response.ok) {
    throw new Error(`GET /api/analyses failed: ${response.status}`)
  }
  return response.json() as Promise<AnalysisCatalogueResponse>
}

/**
 * Uploads a definitions export under a user-chosen analysis ID.
 *
 * Re-uploading the same analysisId within this session replaces the
 * previous upload -- the backend treats this as ordinary versioning, not
 * an error.
 */
export async function uploadDefinitions(
  analysisId: string,
  file: File,
): Promise<UploadResponse> {
  const formData = new FormData()
  formData.append('analysis_id', analysisId)
  formData.append('definitions_file', file)

  const response = await fetch('/api/definitions/upload', {
    method: 'POST',
    headers: { 'X-Session-Token': getSessionToken() },
    body: formData,
  })
  if (!response.ok) {
    throw new Error(`POST /api/definitions/upload failed: ${response.status}`)
  }
  return response.json() as Promise<UploadResponse>
}

/** Deletes one caller-owned session analysis. */
export async function deleteAnalysis(analysisId: string): Promise<void> {
  const encodedAnalysisId = encodeURIComponent(analysisId)
  const response = await fetch(`/api/analyses/${encodedAnalysisId}`, {
    method: 'DELETE',
    headers: { 'X-Session-Token': getSessionToken() },
  })
  if (!response.ok) {
    throw new Error(`DELETE /api/analyses/${encodedAnalysisId} failed: ${response.status}`)
  }
}

/** Pings the backend health check. Used once at startup to confirm the
 * FastAPI backend is reachable through the Vite proxy. */
export async function fetchHealth(): Promise<{ status: string }> {
  const response = await fetch('/api/health')
  if (!response.ok) {
    throw new Error(`GET /api/health failed: ${response.status}`)
  }
  return response.json() as Promise<{ status: string }>
}
