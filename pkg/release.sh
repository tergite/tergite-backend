#!/usr/bin/env bash
# Creates the GitLab Release of this tag, with a link to each package published/ lists: from
# the repo root in the release job. GITLAB_TOKEN, a token with the api scope on this project,
# creates it when set; otherwise the job token does, on a GitLab whose settings allow it.

set -euo pipefail

version=${CI_COMMIT_TAG#v}
api="$CI_API_V4_URL/projects/$CI_PROJECT_ID"
auth=(--header "JOB-TOKEN: $CI_JOB_TOKEN")
[[ -z ${GITLAB_TOKEN:-} ]] || auth=(--header "PRIVATE-TOKEN: $GITLAB_TOKEN")

links='[]'
for file in $(cat published/*); do
	case $file in
	tergite-backend-simulator_*.deb) name="Simulator for Debian 13 and Ubuntu 24.04 onwards (amd64)" ;;
	tergite-backend-simulator-*.pkg) name="Simulator for macOS (Apple silicon)" ;;
	tergite-backend_*.deb) name="Backend for Debian 13 and Ubuntu 24.04 onwards (amd64)" ;;
	tergite-backend-*.pkg) name="Backend for macOS (Apple silicon)" ;;
	*) name=$file ;;
	esac
	links=$(jq --arg name "$name" --arg url "$api/packages/generic/tergite-backend/$version/$file" \
		'. + [{name: $name, url: $url, link_type: "package"}]' <<<"$links")
done

body=$(jq -n --arg tag "$CI_COMMIT_TAG" --arg name "Tergite backend $version" --argjson links "$links" \
	'{tag_name: $tag, name: $name, description: "See CHANGELOG.md.", assets: {links: $links}}')
status=$(curl --silent --show-error --output /tmp/release.json --write-out '%{http_code}' \
	"${auth[@]}" --header "Content-Type: application/json" --data "$body" "$api/releases")
case $status in
201) echo "-> released $CI_COMMIT_TAG" ;;
409)
	# a rerun: the release exists, and GitLab refuses the links it already has
	jq -c '.[]' <<<"$links" | while read -r link; do
		curl --silent --output /dev/null "${auth[@]}" --header "Content-Type: application/json" \
			--data "$link" "$api/releases/$CI_COMMIT_TAG/assets/links" || true
	done
	echo "-> updated $CI_COMMIT_TAG"
	;;
*)
	echo "GitLab answered $status:" >&2
	cat /tmp/release.json >&2
	exit 1
	;;
esac
