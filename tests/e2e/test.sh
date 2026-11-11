#!/usr/bin/env bash
# Run offline checks; verify.py exercises the Docker network
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
TB_PYTHON="${TB_PYTHON:-${DIR}/../../local/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${DIR}${PYTHONPATH:+:${PYTHONPATH}}"
[[ -x "${TB_PYTHON}" ]] || TB_PYTHON=python3
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual
  shift 2
  "$@" >"${TMP}/out" 2>"${TMP}/err"
  actual=$?
  if [[ "${actual}" -eq "${expected}" ]]; then
    printf '[PASS] %s\n' "${name}"
    PASS=$((PASS + 1))
  else
    printf '[FAIL] %s: expected %s, got %s\n' "${name}" "${expected}" "${actual}"
    cat "${TMP}/out" >&2
    cat "${TMP}/err" >&2
    FAIL=$((FAIL + 1))
  fi
}

check "help" 0 "${TB_PYTHON}" "${DIR}/e2e.py" --help
check "version" 0 "${TB_PYTHON}" "${DIR}/e2e.py" --version
check "reject unknown action" 1 "${TB_PYTHON}" "${DIR}/e2e.py" invalid
check "reject bootstrap outside up" 1 "${TB_PYTHON}" "${DIR}/e2e.py" --bootstrap status
check "reject missing action" 1 "${TB_PYTHON}" "${DIR}/e2e.py"
check "node rejects missing role" 1 env -u TB_NODE_ROLE bash "${DIR}/node.sh"
for script in "${DIR}"/*.sh; do
  check "shell syntax: ${script##*/}" 0 bash -n "${script}"
done
check "Python syntax" 0 "${TB_PYTHON}" -c 'import ast,pathlib,sys; [ast.parse(p.read_text()) for p in pathlib.Path(sys.argv[1]).rglob("*.py")]' "${DIR}"
check "configuration and fault cleanup regressions" 0 "${TB_PYTHON}" -m unittest discover -s "${DIR}/unit" -p 'test_*.py'
if command -v shellcheck >/dev/null 2>&1; then
  check "shellcheck" 0 shellcheck "${DIR}"/*.sh
else
  printf '[SKIP] shellcheck is not installed\n' >&2
fi
if LC_ALL=C grep -nE '\$[A-Za-z_][A-Za-z0-9_]*[^ -~]' "${DIR}"/*.sh; then
  echo "[FAIL] unbraced variable before non-ASCII byte"
  FAIL=$((FAIL + 1))
fi
printf 'Result: PASS=%s FAIL=%s\n' "${PASS}" "${FAIL}"
[[ "${FAIL}" -eq 0 ]]
