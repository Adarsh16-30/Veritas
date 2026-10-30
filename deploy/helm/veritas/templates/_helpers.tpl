{{/* Names */}}
{{- define "veritas.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "veritas.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "veritas.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "veritas.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "veritas.selector" -}}
app.kubernetes.io/name: {{ include "veritas.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "veritas.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "veritas.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* image@digest when a digest is pinned, else image:tag */}}
{{- define "veritas.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/* Environment shared by every container: config as env, secrets via envFrom.
     Explicit `env` wins over `envFrom`, so config can override a secret's key.
     Empty values are skipped: an empty string would override a real default.
     Call with (dict "root" . "extra" (dict ...)); `extra` wins over `config`. */}}
{{- define "veritas.env" -}}
{{- $env := merge (dict) (.extra | default dict) .root.Values.config -}}
env:
{{- range $k, $v := $env }}
{{- if ne (toString $v) "" }}
  - name: {{ $k }}
    value: {{ $v | quote }}
{{- end }}
{{- end }}
envFrom:
  - secretRef:
      name: {{ .root.Values.existingSecret }}
{{- end -}}

{{- define "veritas.podSecurity" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
automountServiceAccountToken: false
serviceAccountName: {{ include "veritas.serviceAccountName" . }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "veritas.containerSecurity" -}}
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
{{- end -}}

{{- define "veritas.tmp" -}}
- name: tmp
  emptyDir:
    medium: Memory
    sizeLimit: 16Mi
{{- end -}}

{{- define "veritas.spread" -}}
{{- if .root.Values.topologySpread.enabled }}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: {{ .root.Values.topologySpread.topologyKey }}
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "veritas.selector" . | nindent 8 }}
{{- end }}
{{- end -}}
