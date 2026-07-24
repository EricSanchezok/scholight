#!/bin/sh
set -eu

readonly PLACEHOLDER='@@SCHOLIGHT_PUBLIC_WEB_URL@@'
readonly TEMPLATE_DIR="${1:-/opt/scholight-docs}"
readonly OUTPUT_DIR="${2:-/tmp/scholight-docs}"
readonly PUBLIC_WEB_URL="${SCHOLIGHT_PUBLIC_WEB_URL:-}"

if ! printf '%s\n' "${PUBLIC_WEB_URL}" |
  grep -Eq '^https?://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$'; then
  printf 'Error: SCHOLIGHT_PUBLIC_WEB_URL must be an absolute HTTP(S) origin\n' >&2
  exit 1
fi

for name in docs.md llms.txt; do
  template="${TEMPLATE_DIR}/${name}.template"
  if [ ! -f "${template}" ] || [ -L "${template}" ]; then
    printf 'Error: documentation template is missing: %s\n' "${template}" >&2
    exit 1
  fi
done

umask 022
mkdir -p "${OUTPUT_DIR}"
current_tmp=''
cleanup() {
  if [ -n "${current_tmp}" ]; then
    rm -f -- "${current_tmp}"
  fi
}
trap cleanup EXIT HUP INT TERM

for name in docs.md llms.txt; do
  current_tmp="${OUTPUT_DIR}/.${name}.tmp.$$"
  sed "s|${PLACEHOLDER}|${PUBLIC_WEB_URL}|g" \
    "${TEMPLATE_DIR}/${name}.template" >"${current_tmp}"
  if grep -Fq "${PLACEHOLDER}" "${current_tmp}"; then
    printf 'Error: unresolved documentation URL placeholder in %s\n' "${name}" >&2
    exit 1
  fi
  chmod 0644 "${current_tmp}"
  mv -f -- "${current_tmp}" "${OUTPUT_DIR}/${name}"
  current_tmp=''
done
