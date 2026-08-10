/** Shared wire shapes mirroring the backend's approved Pydantic DTOs. */

export type AnalysisSource = 'static' | 'session'
export type CaptureCompleteness = 'complete' | 'definitions_only'

export interface ClusterSummary {
  clusterId: string | null
  clusterName: string | null
  clusterTags: Readonly<Record<string, string>>
  vhostCount: number
  nodeCount: number
  edgeCount: number
}

export interface AnalysisSummary {
  analysisId: string
  label: string
  source: AnalysisSource
  snapshotRevision: string
  loadedAt: string
  captureCompleteness: CaptureCompleteness
  cluster: ClusterSummary
  description: string | null
  ownerContact: string | null
  expiresAt: string | null
}

export interface AnalysisCatalogueResponse {
  static: ReadonlyArray<AnalysisSummary>
  session: ReadonlyArray<AnalysisSummary>
}

export interface UploadResponse {
  analysis: AnalysisSummary
  replaced: boolean
}

export type AuthMode = 'none' | 'jwt' | 'basic'
export type AppRole = 'viewer' | 'analyst'

export interface AccessResponse {
  authMode: AuthMode
  roles: ReadonlyArray<AppRole>
  capabilities: {
    viewTopology: boolean
    runGraphOperations: boolean
    manageUploads: boolean
  }
}
