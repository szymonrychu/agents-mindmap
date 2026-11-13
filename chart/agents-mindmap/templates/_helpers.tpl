{{- define "agents-mindmap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-mindmap.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agents-mindmap.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-mindmap.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-mindmap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-mindmap.labels" -}}
helm.sh/chart: {{ include "agents-mindmap.chart" . }}
{{ include "agents-mindmap.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "api"). */}}
{{- define "agents-mindmap.componentLabels" -}}
{{ include "agents-mindmap.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "agents-mindmap.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agents-mindmap.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-mindmap.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* "true" when the optional delegate listener is wanted (its token Secret is named). */}}
{{- define "agents-mindmap.delegateEnabled" -}}
{{- if .Values.delegate.tokensSecret.name -}}true{{- end -}}
{{- end }}

{{/* One env entry from an external Secret. Call with (dict "env" NAME "secret" name "key" key). */}}
{{- define "agents-mindmap.secretEnv" -}}
- name: {{ .env }}
  valueFrom:
    secretKeyRef:
      name: {{ .secret | quote }}
      key: {{ .key | quote }}
{{- end }}

{{/* Settings ConfigMap data; no secret values. Fails on a missing required value or bad env key. */}}
{{- define "agents-mindmap.config" -}}
MINDMAP_PUBLIC_BASE_URL: {{ required "agents-mindmap: publicBaseUrl is required (the agents-web base the links carry)" .Values.publicBaseUrl | quote }}
MINDMAP_ORCHESTRATOR_URL: {{ required "agents-mindmap: orchestrator.url is required" .Values.orchestrator.url | quote }}
MINDMAP_HTTP_PORT: {{ .Values.ports.web | int | toString | quote }}
MINDMAP_SERVICE_PORT: {{ .Values.ports.service | int | toString | quote }}
MINDMAP_DELEGATE_PORT: {{ .Values.ports.delegate | int | toString | quote }}
MINDMAP_ALLOWED_CIDRS: {{ join "," .Values.allowedCidrs | quote }}
{{- if .Values.taskboard.url }}
{{- $_ := required "agents-mindmap: taskboard.tokenSecret.name is required when taskboard.url is set" .Values.taskboard.tokenSecret.name }}
{{- end }}
{{- with .Values.embedder.url }}
MINDMAP_EMBEDDER_URL: {{ . | quote }}
{{- end }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "MINDMAP_" $k) }}
{{- fail (printf "agents-mindmap: env key %q must start with MINDMAP_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* Container env: the settings ConfigMap, the database DSN and only the secrets the container
uses (secrets invariant 9). Call with (dict "ctx" . "secrets" (list "orchestrator" "relay" "delegate" "taskboard")). */}}
{{- define "agents-mindmap.env" -}}
{{- $ctx := .ctx -}}
{{- $v := $ctx.Values -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-mindmap.fullname" $ctx }}-settings
env:
  {{- include "agents-mindmap.secretEnv" (dict "env" "MINDMAP_DATABASE_URL" "secret" (required "agents-mindmap: postgres.existingSecret is required" $v.postgres.existingSecret) "key" $v.postgres.existingSecretKey) | nindent 2 }}
  {{- if has "orchestrator" .secrets }}
  {{- include "agents-mindmap.secretEnv" (dict "env" "MINDMAP_ORCHESTRATOR_TOKEN" "secret" (required "agents-mindmap: orchestrator.tokenSecret.name is required" $v.orchestrator.tokenSecret.name) "key" $v.orchestrator.tokenSecret.key) | nindent 2 }}
  {{- end }}
  {{- if has "relay" .secrets }}
  {{- include "agents-mindmap.secretEnv" (dict "env" "MINDMAP_RELAY_TOKEN" "secret" (required "agents-mindmap: relayTokenSecret.name is required" $v.relayTokenSecret.name) "key" $v.relayTokenSecret.key) | nindent 2 }}
  {{- end }}
  {{- if has "delegate" .secrets }}
  {{- include "agents-mindmap.secretEnv" (dict "env" "MINDMAP_DELEGATE_TOKENS" "secret" $v.delegate.tokensSecret.name "key" $v.delegate.tokensSecret.key) | nindent 2 }}
  {{- end }}
  {{- if and (has "taskboard" .secrets) $v.taskboard.url }}
  - name: MINDMAP_TASKBOARD_URL
    value: {{ $v.taskboard.url | quote }}
  {{- include "agents-mindmap.secretEnv" (dict "env" "MINDMAP_TASKBOARD_TOKEN" "secret" (required "agents-mindmap: taskboard.tokenSecret.name is required when taskboard.url is set" $v.taskboard.tokenSecret.name) "key" $v.taskboard.tokenSecret.key) | nindent 2 }}
  {{- end }}
{{- end }}

{{/* The image tag: a semver X.Y.Z or the render fails (never a sha, never latest). */}}
{{- define "agents-mindmap.imageTag" -}}
{{- $tag := .Values.image.tag | toString -}}
{{- if not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+$" $tag) -}}
{{- fail "agents-mindmap: image.tag must be a semver X.Y.Z (deploy only released images)" -}}
{{- end -}}
{{- $tag -}}
{{- end }}

{{/* Pod annotations of the oauth2-proxy admission webhook; fails when a field is missing. */}}
{{- define "agents-mindmap.oauth2Annotations" -}}
{{- if .Values.oauth2Proxy.enabled -}}
oauth2-proxy-admission/enable: "true"
oauth2-proxy-admission/redeem-url: {{ required "agents-mindmap: oauth2Proxy.redeemUrl is required" .Values.oauth2Proxy.redeemUrl | quote }}
oauth2-proxy-admission/secret-name: {{ required "agents-mindmap: oauth2Proxy.secretName is required" .Values.oauth2Proxy.secretName | quote }}
oauth2-proxy-admission/secret-namespace: {{ required "agents-mindmap: oauth2Proxy.secretNamespace is required" .Values.oauth2Proxy.secretNamespace | quote }}
{{- with .Values.oauth2Proxy.apiRoutes }}
oauth2-proxy-admission/api-routes: {{ . | quote }}
{{- end }}
{{- end -}}
{{- end }}
