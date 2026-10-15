#!/usr/bin/env bash
# `A && ok || not_ok` is safe throughout: ok only echoes, so it cannot fail.
# shellcheck disable=SC2015
# Tests for fetch-history.sh against a local upstream (no network). The
# upstream allows fetching any SHA, as GitHub does.
# Run: bash scripts/check-pr-size/fetch-history-test.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${HERE}/fetch-history.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
FAILS=0
ok() { echo "ok - $1"; }
not_ok() { echo "not ok - $1"; FAILS=$((FAILS + 1)); }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
git init -q -b main "$T/up"
cd "$T/up"
git config uploadpack.allowAnySHA1InWant true
for i in $(seq 1 30); do echo "$i" >> f; git add f; git commit -qm "m$i"; done
git checkout -qb feat
for i in 1 2 3; do echo "b$i" >> g; git add g; git commit -qm "b$i"; done
LAST="$(git rev-parse HEAD)"
git checkout -q main
# Push the base branch well past the first rung (depth 100).
for i in $(seq 31 260); do echo "$i" >> f; git commit -qam "m$i"; done
LAST_MB="$(git merge-base main feat)"
git checkout -q feat
echo b4 >> g; git commit -qam b4
HEAD_SHA="$(git rev-parse HEAD)"
BASE_SHA="$(git rev-parse main)"
# A force-pushed-away head: a sibling of LAST that HEAD does not contain.
git checkout -q "$LAST_MB"; echo x >> g; git add g; git commit -qm gone
GONE="$(git rev-parse HEAD)"
git checkout -q feat
cd "$T"

fresh() {
  cd "$T"
  rm -rf "$T/co"
  git clone -q --depth 1 --branch feat "file://$T/up/.git" "$T/co" 2>/dev/null
  cd "$T/co"
}
run() { FETCH_TOKEN=tok BASE_SHA="$1" HEAD_SHA="$2" bash "$SCRIPT" "${@:3}"; }

fresh
if run "$BASE_SHA" "$HEAD_SHA" "$LAST" "$LAST_MB" >"$T/out" 2>&1; then ok "narrowed fetch succeeds"; else not_ok "narrowed fetch succeeds: $(cat "$T/out")"; fi
[ "$(git merge-base "$BASE_SHA" "$HEAD_SHA")" = "$LAST_MB" ] && ok "merge base matches a full clone" || not_ok "merge base matches a full clone"
git merge-base --is-ancestor "$LAST_MB" "$LAST" && git merge-base --is-ancestor "$LAST" "$HEAD_SHA" \
  && ok "extras' ancestry resolves" || not_ok "extras' ancestry resolves"
if grep -qi 'extraheader\|authorization' .git/config; then not_ok "no credential in .git/config"; else ok "no credential in .git/config"; fi
grep -q '::add-mask::' "$T/out" && ok "credential is masked" || not_ok "credential is masked"

fresh
run "$BASE_SHA" "$HEAD_SHA" "$GONE" "$LAST_MB" >"$T/out" 2>&1 || true
if git cat-file -e "${GONE}^{commit}" 2>/dev/null && ! git merge-base --is-ancestor "$GONE" "$HEAD_SHA"; then
  ok "a force-pushed-away head is fetched but is not an ancestor of HEAD"
else
  not_ok "a force-pushed-away head is fetched but is not an ancestor of HEAD"
fi

fresh
if run "$BASE_SHA" "$HEAD_SHA" "0123456789abcdef0123456789abcdef01234567" "--upload-pack=evil" "" >"$T/out" 2>&1; then
  ok "unfetchable / malformed / empty extras are not fatal"
else
  not_ok "unfetchable / malformed / empty extras are not fatal: $(cat "$T/out")"
fi
grep -q "Ignoring extra commit argument #2: not a full 40-hex SHA" "$T/out" && ok "a non-SHA extra never reaches git" || not_ok "a non-SHA extra never reaches git"
grep -q -- '--upload-pack=evil' "$T/out" && not_ok "a rejected extra is not echoed" || ok "a rejected extra is not echoed"

fresh
if run "$BASE_SHA" "$HEAD_SHA" "$(printf '%s\n::error::x' "$LAST")" >"$T/out" 2>&1; then
  ok "a multi-line extra is not fatal"
else
  not_ok "a multi-line extra is not fatal: $(cat "$T/out")"
fi
grep -q '^::error::x' "$T/out" && not_ok "a multi-line extra is rejected whole, never echoed" || ok "a multi-line extra is rejected whole, never echoed"

fresh
if run "$BASE_SHA" "0123456789abcdef0123456789abcdef01234567" >"$T/out" 2>&1; then
  not_ok "an unfetchable HEAD fails"
else
  grep -q '::error::could not fetch the history' "$T/out" && ok "an unfetchable HEAD fails, naming the fetch" || not_ok "an unfetchable HEAD fails, naming the fetch: $(cat "$T/out")"
fi

echo
if [ "$FAILS" -ne 0 ]; then echo "$FAILS failed"; exit 1; fi
echo "all passed"
