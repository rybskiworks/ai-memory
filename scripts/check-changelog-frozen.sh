#!/usr/bin/env bash
# Fail if a change adds or edits entries inside an already-released
# CHANGELOG section.
#
# `bin/release` renames `## [Unreleased]` to `## [X.Y.Z]`. A branch opened
# before that release still carries a CHANGELOG diff anchored at the old
# `[Unreleased]` line numbers, and git merges it into whatever section now
# occupies them — no conflict, no warning. The entry then claims a change
# shipped in a release that did not contain it, and the release that *did*
# ship it lists nothing.
#
# Three times in one week: #491 into [1.32.0], #502 into [1.32.2], #517 into
# [1.33.0]. Each found by hand, twice only because an unrelated merge
# conflict forced someone to look at the file.
#
# Usage: check-changelog-frozen.sh [base-ref]   (default: origin/main)
#
# Compares existing released prose on <base-ref> with HEAD. Newer releases and
# the trailing Markdown reference links may advance without rewriting older
# release entries.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BASE_REF="${1:-origin/main}"
git rev-parse --verify "$BASE_REF" >/dev/null 2>&1 || {
    echo "check-changelog-frozen: base ref '$BASE_REF' not found; skipping."
    exit 0
}

# Compare the released half of the file directly against the base branch,
# rather than diffing line ranges since the merge base.
#
# The line-range form fired falsely on any branch that merged the base after a
# release: from the old merge base, the whole new `## [X.Y.Z]` section reads as
# lines this branch touched, even though it arrived through the merge. The
# question that matters is simpler — does this branch's copy of an
# already-released section differ from the base branch's copy?
#
# A section the base has and this branch does not is fine (a stale branch, not
# a rewrite), so only sections present in both are compared.

released_half() {
    git show "$1:CHANGELOG.md" 2>/dev/null | awk '
        /^## \[[0-9]/ { released = 1 }
        released {
            # Reference links at EOF describe the whole changelog. In particular,
            # the Unreleased comparison advances whenever a new release lands.
            # Buffer a possible footer; flush it if more prose follows so an
            # embedded definition cannot hide edits to historical entries.
            if ($0 ~ /^\[[^]]+\]:[[:space:]]+[^[:space:]]/) {
                pending = pending $0 ORS
                references = 1
                next
            }
            if ($0 ~ /^[[:space:]]*$/) {
                pending = pending $0 ORS
                next
            }
            printf "%s", pending
            pending = ""
            references = 0
            print
        }
        END { if (!references) printf "%s", pending }
    '
}

BASE_RELEASED="$(released_half "$BASE_REF")"
HEAD_RELEASED="$(released_half HEAD)"

if [[ -z "$BASE_RELEASED" ]]; then
    echo "check-changelog-frozen: base has no released section yet."
    exit 0
fi

# The newest released heading on the base. Anything at or below it in HEAD must
# match the base byte for byte.
# `head -1` under `pipefail` exits 141 on SIGPIPE, so read the first line
# without a pipe.
NEWEST="${BASE_RELEASED%%$'\n'*}"
# Substring tests, not pipes: `grep -q` closes the pipe on its first match and
# `pipefail` reports that SIGPIPE as failure, which made this branch look like
# it predated the release.
if [[ "$HEAD_RELEASED" != *"$NEWEST"* ]]; then
    echo "check-changelog-frozen: HEAD predates $NEWEST; nothing to compare."
    exit 0
fi

HEAD_FROM_NEWEST="${HEAD_RELEASED#*"$NEWEST"}"
HEAD_FROM_NEWEST="${NEWEST}${HEAD_FROM_NEWEST}"

if [[ "$HEAD_FROM_NEWEST" == "$BASE_RELEASED" ]]; then
    echo "CHANGELOG: no released section was modified."
    exit 0
fi

echo "error: this change modifies an already-released CHANGELOG section." >&2
echo >&2
diff <(printf '%s\n' "$BASE_RELEASED") <(printf '%s\n' "$HEAD_FROM_NEWEST") | head -40 >&2
cat >&2 <<'EOF'

Released sections are frozen. An entry for unreleased work belongs under
`## [Unreleased]`.

This usually means the branch was opened before a release was cut, and git
merged the entry into the section that now occupies those lines. Move it up
to `[Unreleased]`; the content does not change, only the section.
EOF
exit 1
