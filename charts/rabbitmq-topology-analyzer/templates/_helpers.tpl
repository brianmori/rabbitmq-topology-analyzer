{{- define "analyzer.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "analyzer.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "analyzer.labels" -}}
{{ include "analyzer.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}

{{- define "analyzer.publicOrigin" -}}
{{- .Values.auth.publicOrigin | lower | trimSuffix ":443" -}}
{{- end -}}

{{- define "analyzer.validateStorage" -}}
{{- $s := .Values.storage -}}
{{- if ge (int64 $s.resultsMaxBytes) (int64 $s.resultsVolumeBytes) -}}
{{- fail "storage.resultsMaxBytes must be below storage.resultsVolumeBytes" -}}
{{- end -}}
{{- if ge (add (int64 $s.resultsVolumeBytes) (int64 $s.temporaryVolumeBytes)) (int64 $s.ephemeralLimitBytes) -}}
{{- fail "storage volumes must leave headroom below storage.ephemeralLimitBytes" -}}
{{- end -}}
{{- if gt (int64 $s.ephemeralRequestBytes) (int64 $s.ephemeralLimitBytes) -}}
{{- fail "storage.ephemeralRequestBytes must not exceed storage.ephemeralLimitBytes" -}}
{{- end -}}
{{- end -}}
