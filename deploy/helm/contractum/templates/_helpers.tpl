{{/* Image reference for a platform component: [registry/]contractum/<name>:<tag>. The default
registry is ghcr.io/rkisliak, where .github/workflows/release.yml publishes the images; the tag
defaults to the chart's appVersion, the release that published them. */}}
{{- define "dc.image" -}}
{{- $ctx := index . 0 -}}{{- $name := index . 1 -}}
{{- $tag := $ctx.Values.global.imageTag | default $ctx.Chart.AppVersion -}}
{{- if $ctx.Values.global.imageRegistry -}}
{{ printf "%s/contractum/%s:%s" $ctx.Values.global.imageRegistry $name $tag }}
{{- else -}}
{{ printf "contractum/%s:%s" $name $tag }}
{{- end -}}
{{- end -}}

{{- define "dc.labels" -}}
app.kubernetes.io/part-of: contractum
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Selector labels for one component. Usage: include "dc.selector" (list . "identity") */}}
{{- define "dc.selector" -}}
app.kubernetes.io/name: {{ index . 1 }}
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
{{- end -}}

{{- define "dc.pullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{/* The service images run as the unprivileged app user 10001 (infra/docker/service.Dockerfile). */}}
{{- define "dc.appPodSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "dc.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{/* Environment shared by every Python service (x-app-env in compose.yaml). */}}
{{- define "dc.appEnv" -}}
- name: DC_NATS_URL
  value: {{ .Values.nats.url | quote }}
- name: DC_AUTH_IDENTITY_INTERNAL_URL
  value: "http://identity:8000"
- name: DC_AUTH_ISSUERS
  value: {{ .Values.auth.issuers | toJson | quote }}
- name: DC_LOG_LEVEL
  value: {{ .Values.global.logLevel | quote }}
{{- end -}}

{{/* Database environment for one service. Usage: include "dc.dbEnv" (list . "identity") */}}
{{- define "dc.dbEnv" -}}
{{- $ctx := index . 0 -}}{{- $svc := index . 1 -}}
- name: DC_DB_HOST
  value: {{ $ctx.Values.postgres.host | quote }}
- name: DC_DB_PORT
  value: {{ $ctx.Values.postgres.port | quote }}
- name: DC_DB_NAME
  value: {{ $svc | quote }}
- name: DC_DB_USER
  value: {{ $svc | quote }}
{{- end -}}

{{/* The service's Secret, mounted read-only at /run/secrets (the compose layout). */}}
{{- define "dc.secretVolume" -}}
{{- $ctx := index . 0 -}}{{- $svc := index . 1 -}}
- name: secrets
  secret:
    secretName: {{ index $ctx.Values.secrets $svc }}
    defaultMode: 0440
{{- end -}}

{{- define "dc.uvicorn" -}}
{{- $ctx := index . 0 -}}{{- $factory := index . 1 -}}
- uvicorn
- --factory
- {{ $factory }}
- --host
- 0.0.0.0
- --port
- "8000"
- --proxy-headers
- --forwarded-allow-ips
- {{ $ctx.Values.global.trustedProxies | quote }}
- --no-server-header
{{- end -}}

{{- define "dc.httpProbes" -}}
{{- $port := . -}}
readinessProbe:
  httpGet: {path: /healthz, port: {{ $port }}}
  periodSeconds: 5
  timeoutSeconds: 5
  failureThreshold: 3
livenessProbe:
  httpGet: {path: /healthz, port: {{ $port }}}
  initialDelaySeconds: 20
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 4
{{- end -}}
