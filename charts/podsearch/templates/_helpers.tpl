{{- /* Env for every pod running our Python code (Ray head and workers, later the controller and API). */ -}}
{{- define "podsearch.appEnv" -}}
- {name: S3_ENDPOINT, value: "http://rustfs:9000"}
- name: S3_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: s3-credentials, key: access_key}}
- name: S3_SECRET_KEY
  valueFrom: {secretKeyRef: {name: s3-credentials, key: secret_key}}
- {name: QDRANT_URL, value: "http://qdrant:6333"}
- {name: EMBED_ACTORS_MAX, value: {{ .Values.ray.embedActorsMax | quote }}}
{{- end -}}
