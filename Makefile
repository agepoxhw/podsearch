.PHONY: prereqs up down status logs upload upload-all load ls test lock images
DATA_DIR ?= ./source_data
SHELL := /bin/bash

prereqs:            ## check host tools and Docker resources
	@scripts/prereqs.sh

up: prereqs         ## create/start the cluster and deploy everything (idempotent)
	@scripts/up.sh

down:               ## destroy the cluster and its registry
	k3d cluster delete podsearch

status:             ## what is running
	@kubectl get nodes
	@kubectl -n podsearch get all,cm,secret,pvc 2>/dev/null || true

logs:               ## tail logs of all pods in the namespace
	kubectl -n podsearch logs --all-containers --prefix --tail=50 -l app.kubernetes.io/part-of=podsearch

upload:             ## upload one file: make upload FILE=path/to/x.mp3
	@test -n "$(FILE)" || { echo "usage: make upload FILE=path/to/x.mp3"; exit 2; }
	@scripts/upload.sh "$(FILE)"

upload-all:         ## upload every mp3 in DATA_DIR (default ./source_data)
	@scripts/upload.sh $(DATA_DIR)/*.mp3

load: upload-all    ## alias: load the sample data set

ls:                 ## list the bucket (incoming/, library/, rejected/)
	@kubectl -n podsearch exec deploy/toolbox -- aws s3 ls s3://podcasts/ --recursive --human-readable

lock:               ## regenerate images/ml-base/requirements.<arch>.txt
	@scripts/lock.sh

test:               ## unit tests, run inside the ml-base image (same libs as production)
	@scripts/test.sh

images:             ## build ml-base/ml-speech/ml-text for this machine and push to the local registry
	@scripts/build-images.sh
