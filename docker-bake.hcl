# Build an agent's container image for AgentCore Runtime and push it to your Amazon ECR.
#
#   export REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com
#   docker buildx bake claude-code --push        # from the repository root; or codex, kiro, ...
#
# Every Runtime runs on Graviton, so every image is linux/arm64. Two named contexts
# carry the shared pieces: "toolchain" is coding-agents/ (the pinned CLI versions and
# their checker, cli-versions.json and cli_versions.py) and "skills" holds the
# engineering skills the builders read. Every path stays inside this repository, so
# bake needs no extra filesystem entitlement. The base image is pinned by digest and must
# equal runtime.base_image in cli-versions.json (e2e/test_runtime_base_image.py).

variable "REGISTRY" {}

variable "BASE_IMAGE" {
  default = "public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:06da5a3362eda00c5114227ac81abe56dd9b943395553b7c64a310457f4d9e2b"
}

target "_agent" {
  platforms = ["linux/arm64"]
  args      = { WORKSHOP_RUNTIME_BASE_IMAGE = BASE_IMAGE }
  contexts  = { toolchain = "coding-agents", skills = "harness-skills/skills" }
}

target "claude-code" {
  inherits = ["_agent"]
  context  = "coding-agents/claude-code"
  tags     = ["${REGISTRY}/coding-agents-claude-code:latest"]
}

target "codex" {
  inherits = ["_agent"]
  context  = "coding-agents/codex"
  tags     = ["${REGISTRY}/coding-agents-codex:latest"]
}

target "kiro" {
  inherits = ["_agent"]
  context  = "coding-agents/kiro"
  tags     = ["${REGISTRY}/coding-agents-kiro:latest"]
}

target "claude-code-validator" {
  inherits = ["_agent"]
  context  = "coding-agents/claude-code-validator"
  tags     = ["${REGISTRY}/coding-agents-claude-code-validator:latest"]
}

target "opencode" {
  inherits = ["_agent"]
  context  = "coding-agents/opencode"
  tags     = ["${REGISTRY}/coding-agents-opencode:latest"]
}
