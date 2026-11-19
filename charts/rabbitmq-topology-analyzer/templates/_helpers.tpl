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
{{- $origin := .Values.auth.publicOrigin | lower -}}
{{- if not (regexMatch `^https://([a-z0-9.-]+|\[[0-9a-f:.]+\])(:[0-9]+)?$` $origin) -}}
{{- fail "auth.publicOrigin must be an HTTPS origin with an ASCII hostname (use Punycode for internationalized names) or a canonical IP address" -}}
{{- end -}}
{{- $portSuffix := regexFind `:[0-9]+$` $origin -}}
{{- if $portSuffix -}}
{{- $port := $portSuffix | trimPrefix ":" | atoi -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail "auth.publicOrigin port must be between 1 and 65535" -}}
{{- end -}}
{{- $origin = trimSuffix $portSuffix $origin -}}
{{- if ne $port 443 -}}
{{- $origin = printf "%s:%d" $origin $port -}}
{{- end -}}
{{- end -}}
{{- $origin -}}
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
