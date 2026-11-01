IMAGE ?= pr-reviewer:local
VERSION ?= 0.1.1

.PHONY: build test check logs up down runner-images publish
build:
	docker build --target release --build-arg VERSION=$(VERSION) -t $(IMAGE) .
test:
	docker build --target test -t pr-reviewer:test .
check:
	docker compose run --rm reviewer check
up:
	docker compose up -d
down:
	docker compose down
logs:
	docker compose logs -f --tail=100 reviewer
runner-images:
	docker build -f runner-node.Dockerfile -t pr-reviewer-node:1 .
	docker build -f runner-python.Dockerfile -t pr-reviewer-python:1 .
publish:
	test -n "$(REGISTRY_IMAGE)"
	docker buildx build --platform linux/amd64,linux/arm64 --target release --build-arg VERSION=$(VERSION) \
	  -t $(REGISTRY_IMAGE):$(VERSION) -t $(REGISTRY_IMAGE):latest --push .
