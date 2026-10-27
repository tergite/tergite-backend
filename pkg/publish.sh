#!/usr/bin/env bash
# Uploads packages into the project's generic package registry, as tergite-backend under the
# tag's version, and lists them in published/ for the release job: from the repo root in a tag's
# pipeline, `pkg/publish.sh dist/*.deb`. Each job uploads what it built, as the build is
# bigger than GitLab's default artifact limit of 100 MB. CI provides CI_API_V4_URL,
# CI_PROJECT_ID, CI_JOB_TOKEN, CI_JOB_ID and CI_COMMIT_TAG.

set -euo pipefail

(($#)) || {
	echo "nothing to publish" >&2
	exit 1
}
registry="$CI_API_V4_URL/projects/$CI_PROJECT_ID/packages/generic/tergite-backend/${CI_COMMIT_TAG#v}"

mkdir -p published
for file in "$@"; do
	echo "-> $(basename "$file")"
	# the body says which limit refused it: an nginx page or GitLab's own message
	curl --fail-with-body --silent --show-error --header "JOB-TOKEN: $CI_JOB_TOKEN" \
		--write-out '\n' --upload-file "$file" "$registry/$(basename "$file")"
	basename "$file" >>"published/$CI_JOB_ID"
done
