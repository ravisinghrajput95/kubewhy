{{/*
Inference configuration, in one place because two workloads need it.

The controller and the UI both run the agent loop, so both need the same
answer to "where does inference happen and may evidence leave to get there".
Rendering it twice by hand is how the UI ends up talking to a different model
than the controller for a release nobody notices.
*/}}

{{- define "kubewhy.inference.validate" -}}
{{- $inf := .Values.inference -}}
{{- if not (has $inf.mode (list "local" "cluster" "api")) }}
  {{- fail (printf "\n\ninference.mode is %q; expected local, cluster or api.\n" $inf.mode) }}
{{- end }}

{{- if and (eq $inf.mode "api") (not $inf.allowExternal) }}
  {{- fail "\n\ninference.mode is 'api' but inference.allowExternal is false.\n\nApi mode means cluster evidence -- pod logs included -- leaves your network\nto reach a hosted model. That is a decision, so it has to be made explicitly\nrather than implied by choosing a mode.\n\nThe agent would refuse this configuration at startup anyway. Failing here\ninstead means you find out from `helm install` rather than from a\nCrashLoopBackOff.\n\nSet inference.allowExternal=true if that is what you want.\n" }}
{{- end }}

{{- if $inf.fallback.enabled }}
  {{- if not $inf.fallback.model }}
    {{- fail "\n\ninference.fallback.enabled is true but inference.fallback.model is empty.\n\nA fallback is a different provider serving a different catalogue, so it needs\nits own model name. Inheriting the primary's would produce a 404 at the one\nmoment the primary is already down.\n" }}
  {{- end }}
  {{- if and (eq $inf.fallback.mode "api") (not $inf.allowExternal) }}
    {{- fail "\n\ninference.fallback.mode is 'api' but inference.allowExternal is false.\n\nA fallback is not a way around the external-data policy: it sends the same\nevidence to the same kind of place. Set inference.allowExternal=true, or point\nthe fallback at something on your network.\n" }}
  {{- end }}
{{- end }}

{{- if and .Values.networkPolicy.enabled (eq $inf.mode "api") }}
  {{- fail "\n\nnetworkPolicy.enabled and inference.mode='api' say opposite things.\n\nThe policy denies egress to the public internet; api mode requires it. One of\nthe two has to go: use an in-cluster or on-network endpoint, or add its range\nto networkPolicy.extraAllowedCIDRs and set inference.mode accordingly.\n" }}
{{- end }}
{{- end -}}


{{/*
The environment every kubewhy workload needs to reach a model.

OLLAMA_HOST is still emitted and still honoured. An existing values file that
only ever set model.ollamaHost keeps working exactly as it did: inference.py
reads it when inference.endpoint is empty, which is also what lets the egress
check see the address at all rather than having the client pick it up quietly.
*/}}
{{- define "kubewhy.inference.env" -}}
{{- $inf := .Values.inference -}}
- name: TRIAGE_INFERENCE_MODE
  value: {{ $inf.mode | quote }}
{{- if $inf.provider }}
- name: TRIAGE_INFERENCE_PROVIDER
  value: {{ $inf.provider | quote }}
{{- end }}
{{- if $inf.endpoint }}
- name: TRIAGE_INFERENCE_ENDPOINT
  value: {{ $inf.endpoint | quote }}
{{- end }}
- name: OLLAMA_HOST
  value: {{ .Values.model.ollamaHost | quote }}
- name: TRIAGE_MODEL
  value: {{ $inf.model | default .Values.model.name | quote }}
- name: OLLAMA_TIMEOUT
  value: {{ .Values.model.timeoutSeconds | quote }}
- name: TRIAGE_ALLOW_EXTERNAL_INFERENCE
  value: {{ $inf.allowExternal | quote }}
- name: TRIAGE_REDACT_ON_EGRESS
  value: {{ $inf.redactOnEgress | quote }}
{{- if or $inf.apiKey.existingSecret $inf.apiKey.value }}
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $inf.apiKey.existingSecret | default (printf "%s-inference" .Release.Name) }}
      key: {{ $inf.apiKey.key }}
      # Optional so a rotation that briefly removes the key drains the pod
      # rather than wedging it in CreateContainerConfigError -- which is a
      # state describe_pod reports and nothing else explains.
      optional: true
{{- end }}
{{- if $inf.fallback.enabled }}
- name: TRIAGE_FALLBACK_ENABLED
  value: "true"
- name: TRIAGE_FALLBACK_MODE
  value: {{ $inf.fallback.mode | quote }}
{{- if $inf.fallback.provider }}
- name: TRIAGE_FALLBACK_PROVIDER
  value: {{ $inf.fallback.provider | quote }}
{{- end }}
{{- if $inf.fallback.endpoint }}
- name: TRIAGE_FALLBACK_ENDPOINT
  value: {{ $inf.fallback.endpoint | quote }}
{{- end }}
- name: TRIAGE_FALLBACK_MODEL
  value: {{ $inf.fallback.model | quote }}
{{- if or $inf.fallback.apiKey.existingSecret $inf.fallback.apiKey.value }}
- name: TRIAGE_FALLBACK_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $inf.fallback.apiKey.existingSecret | default (printf "%s-inference" .Release.Name) }}
      key: {{ $inf.fallback.apiKey.key }}
      optional: true
{{- end }}
{{- end }}
{{- end -}}
