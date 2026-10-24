.PHONY: prereqs up down status logs
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
