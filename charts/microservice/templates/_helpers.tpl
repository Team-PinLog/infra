{{/*
서비스 이름.
차트 이름(microservice)이 아니라 Release 이름을 기본값으로 쓴다.
차트 이름을 쓰면 모든 서비스가 app.kubernetes.io/name=microservice 가 되어
레이블로 특정 서비스를 조회할 수 없다.
*/}}
{{- define "microservice.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
전체 이름. ApplicationSet이 Release 이름을 디렉터리 이름으로 넘기므로
apps/prod/auth-service/ → auth-service 가 된다.
*/}}
{{- define "microservice.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Metadata label values sourced from versions must be DNS-label safe. Preserve short,
already-safe values; otherwise retain a readable prefix plus a collision-resistant hash.
*/}}
{{- define "microservice.labelValue" -}}
{{- $raw := . | toString -}}
{{- if and (le (len $raw) 63) (regexMatch "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$" $raw) -}}
{{- $raw -}}
{{- else -}}
{{- $normalized := regexReplaceAll "[^a-z0-9-]+" ($raw | lower) "-" | trimAll "-" -}}
{{- $prefix := $normalized | trunc 50 | trimSuffix "-" -}}
{{- if eq $prefix "" -}}
{{- $prefix = "value" -}}
{{- end -}}
{{- printf "%s-%s" $prefix ($raw | sha256sum | trunc 12) -}}
{{- end -}}
{{- end }}

{{- define "microservice.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "microservice.version" -}}
{{- include "microservice.labelValue" (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "microservice.labels" -}}
helm.sh/chart: {{ include "microservice.chart" . }}
{{ include "microservice.selectorLabels" . }}
app.kubernetes.io/version: {{ include "microservice.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: pinlog
{{- end }}

{{- define "microservice.selectorLabels" -}}
app.kubernetes.io/name: {{ include "microservice.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "microservice.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "microservice.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
