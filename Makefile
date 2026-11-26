
VERSION=0.0.1

CONTAINER_BASE=docker.io/trustgraph
DOCKER=podman

all: container

container-%:
	${DOCKER} build \
	    -f Containerfile.${@:container-%=%} \
	    -t ${CONTAINER_BASE}/trustgraph-${@:container-%=%}:${VERSION} .

container: container-duckdb-action-service

manifest-%:
	-@${DOCKER} manifest rm \
	    ${CONTAINER_BASE}/trustgraph-${@:manifest-%=%}:${VERSION}
	${DOCKER} build --platform linux/amd64,linux/arm64 \
	    -f Containerfile.${@:manifest-%=%} \
	    --manifest \
	    ${CONTAINER_BASE}/trustgraph-${@:manifest-%=%}:${VERSION} .

platform-%-amd64:
	${DOCKER} build --platform linux/amd64 \
	    -f Containerfile.${@:platform-%-amd64=%} \
	    -t ${CONTAINER_BASE}/trustgraph-${@:platform-%-amd64=%}:${VERSION}-amd64 .

platform-%-arm64:
	${DOCKER} build --platform linux/arm64 \
	    -f Containerfile.${@:platform-%-arm64=%} \
	    -t ${CONTAINER_BASE}/trustgraph-${@:platform-%-arm64=%}:${VERSION}-arm64 .

push-platform-%-amd64:
	${DOCKER} push \
	    ${CONTAINER_BASE}/trustgraph-${@:push-platform-%-amd64=%}:${VERSION}-amd64

push-platform-%-arm64:
	${DOCKER} push \
	    ${CONTAINER_BASE}/trustgraph-${@:push-platform-%-arm64=%}:${VERSION}-arm64

combine-manifest-%:
	-@${DOCKER} manifest rm \
	    ${CONTAINER_BASE}/trustgraph-${@:combine-manifest-%=%}:${VERSION}
	${DOCKER} manifest create \
	    ${CONTAINER_BASE}/trustgraph-${@:combine-manifest-%=%}:${VERSION} \
	    docker://${CONTAINER_BASE}/trustgraph-${@:combine-manifest-%=%}:${VERSION}-amd64 \
	    docker://${CONTAINER_BASE}/trustgraph-${@:combine-manifest-%=%}:${VERSION}-arm64
	${DOCKER} manifest push \
	    ${CONTAINER_BASE}/trustgraph-${@:combine-manifest-%=%}:${VERSION}

push-container-%:
	${DOCKER} push \
	    ${CONTAINER_BASE}/trustgraph-${@:push-container-%=%}:${VERSION}

push-manifest-%:
	${DOCKER} manifest push \
	    ${CONTAINER_BASE}/trustgraph-${@:push-manifest-%=%}:${VERSION}

push: push-container-duckdb-action-service

docker-hub-login:
	cat docker-token.txt | \
	    ${DOCKER} login -u trustgraph --password-stdin registry-1.docker.io
