{{/* vim: set filetype=mustache: */}}
{{/*

{{/*
Expand the name of the chart.
*/}}
{{- define "simple-pushgateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name
*/}}
{{- define "simple-pushgateway.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "simple-pushgateway.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "simple-pushgateway.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/name: {{ template "simple-pushgateway.name" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ template "simple-pushgateway.chart" .  }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "simple-pushgateway.selectorLabels" -}}
app.kubernetes.io/name: {{ template "simple-pushgateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}


{{/*
Pdb apiVersion according k8s version and capabilities
*/}}
{{- define "simple-pushgateway.pdb.apiVersion" -}}
{{- if and (.Capabilities.APIVersions.Has "policy/v1") (semverCompare ">=1.21-0" .Capabilities.KubeVersion.GitVersion) -}}
policy/v1
{{- else -}}
policy/v1beta1
{{- end }}
{{- end -}}