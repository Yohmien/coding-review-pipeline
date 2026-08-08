#!/usr/bin/env bash
set -euo pipefail

SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$SKILLS_DIR"

install_local() {
  local src="$1" name="$2"
  if [ -d "$SKILLS_DIR/$name" ]; then
    echo "skip $name (already installed)"
  else
    cp -R "$src" "$SKILLS_DIR/$name"
    echo "installed $name"
  fi
}

install_from_repo() {
  local repo="$1" path="$2" name="$3"
  if [ -d "$SKILLS_DIR/$name" ]; then
    echo "skip $name (already installed)"
    return
  fi
  local tmp
  tmp="$(mktemp -d)"
  if ! git clone --depth 1 "$repo" "$tmp/repo" >/dev/null 2>&1; then
    echo "error: failed to clone $repo" >&2
    rm -rf "$tmp"
    exit 1
  fi
  mkdir -p "$SKILLS_DIR"
  if [ "$path" = "." ]; then
    cp -R "$tmp/repo/." "$SKILLS_DIR/$name"
  else
    cp -R "$tmp/repo/$path" "$SKILLS_DIR/$name"
  fi
  rm -rf "$tmp"
  rm -rf "$SKILLS_DIR/$name/.git"
  echo "installed $name"
}

install_local "$REPO_ROOT/skills/coding-review-pipeline" coding-review-pipeline
install_local "$REPO_ROOT/vendor/skills/search-gates" search-gates
install_from_repo https://github.com/mattpocock/skills.git skills/engineering/grill-with-docs grill-with-docs
install_from_repo https://github.com/mattpocock/skills.git skills/productivity/grilling grilling
install_from_repo https://github.com/mattpocock/skills.git skills/engineering/domain-modeling domain-modeling
install_from_repo https://github.com/obra/superpowers.git skills/verification-before-completion verification-before-completion
install_from_repo https://github.com/obra/superpowers.git skills/systematic-debugging systematic-debugging
install_from_repo https://github.com/obra/superpowers.git skills/test-driven-development test-driven-development
install_from_repo https://github.com/DietrichGebert/ponytail.git skills/ponytail ponytail
install_from_repo https://github.com/Sxuan-Coder/alibaba-java-development-guide.git . alibaba-java-development-guide

echo ""
echo "Done. All skills installed to $SKILLS_DIR"
