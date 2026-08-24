#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Install Goal Plus for the reusable ThinkThread Pi Profile.

Usage:
  scripts/install_pi_goal_plus_thinkthread.sh \
    (--thinkthread-source PATH | --agent-posix-package FILE) \
    [--model PROVIDER/MODEL ...] [--python PYTHON] [--tt TT]

The installation is self-contained under ~/.local/share/goal-plus. The
Profile is installed as ~/.config/thinkthread/profiles/pi-goal-plus.toml and
reuses ~/.pi/agent without modifying that ordinary Pi configuration.
After installation, launch it from any target workspace with:
  tt pi-goal-plus

Options:
  --thinkthread-source PATH   ThinkThread checkout containing sdk/agent-posix/ts
  --agent-posix-package FILE  Prebuilt @thinkthread/agent-posix .tgz package
  --model PROVIDER/MODEL      Delegate one exact Child model; repeatable
  --python PYTHON             Python used to create the private venv (python3)
  --tt TT                     Current ThinkThread host CLI. Prefers /usr/bin/tt
  -h, --help                  Show this help
EOF
}

die() {
    printf 'install_pi_goal_plus_thinkthread: %s\n' "$*" >&2
    exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "$script_dir/.." && pwd -P)"
install_root="$HOME/.local/share/goal-plus"
profile_root="${XDG_CONFIG_HOME:-$HOME/.config}/thinkthread/profiles"
profile_path="$profile_root/pi-goal-plus.toml"
python_bin="python3"
tt_bin="${GOAL_PLUS_TT:-}"
thinkthread_source=""
sdk_package=""
requested_models=()

while (($# > 0)); do
    case "$1" in
        --thinkthread-source)
            (($# >= 2)) || die "$1 requires a path"
            thinkthread_source="$2"
            shift 2
            ;;
        --agent-posix-package)
            (($# >= 2)) || die "$1 requires a file"
            sdk_package="$2"
            shift 2
            ;;
        --model)
            (($# >= 2)) || die "$1 requires provider/model"
            [[ "$2" == */* && "$2" != */ && "$2" != /* ]] || die "invalid model: $2"
            requested_models+=("$2")
            shift 2
            ;;
        --python)
            (($# >= 2)) || die "$1 requires a command"
            python_bin="$2"
            shift 2
            ;;
        --tt)
            (($# >= 2)) || die "$1 requires a command"
            tt_bin="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

if [[ -n "$thinkthread_source" && -n "$sdk_package" ]]; then
    die "choose either --thinkthread-source or --agent-posix-package"
fi
if [[ -z "$thinkthread_source" && -z "$sdk_package" ]]; then
    die "the official Agent POSIX SDK source or package is required"
fi

command -v "$python_bin" >/dev/null 2>&1 || die "Python is unavailable: $python_bin"
command -v node >/dev/null 2>&1 || die "Node.js 20+ is required"
command -v npm >/dev/null 2>&1 || die "npm is required to install the official SDK package"
node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
((node_major >= 20)) || die "Node.js 20+ is required; found $(node --version)"

if [[ -z "$tt_bin" ]]; then
    if [[ -x /usr/bin/tt ]]; then
        tt_bin=/usr/bin/tt
    else
        tt_bin="$(command -v tt || true)"
    fi
fi
[[ -n "$tt_bin" && -x "$tt_bin" ]] || die "the current ThinkThread tt binary is unavailable"

mkdir -p -- "$(dirname -- "$install_root")" "$profile_root"
transaction_root="$(mktemp -d "$(dirname -- "$install_root")/.goal-plus-install.XXXXXX")"
payload_root="$transaction_root/payload"
previous_install="$transaction_root/previous-install"
previous_profile="$transaction_root/previous-profile.toml"
installed_new=false
profile_replaced=false
success=false

rollback_and_cleanup() {
    status=$?
    if [[ "$success" != true ]]; then
        if [[ "$profile_replaced" == true ]]; then
            rm -f -- "$profile_path"
            if [[ -f "$previous_profile" ]]; then
                mv -- "$previous_profile" "$profile_path"
            fi
        fi
        if [[ "$installed_new" == true ]]; then
            rm -rf -- "$install_root"
            if [[ -d "$previous_install" ]]; then
                mv -- "$previous_install" "$install_root"
            fi
        fi
    fi
    rm -rf -- "$transaction_root"
    exit "$status"
}
trap rollback_and_cleanup EXIT

mkdir -p -- "$payload_root/bin" "$payload_root/pi/goal-plus" "$payload_root/bridge"
"$python_bin" -m venv "$payload_root/venv"
"$payload_root/venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input "$repository_root"

install -m 0644 "$repository_root/.pi/extensions/goal-plus.ts" \
    "$payload_root/pi/goal-plus.ts"
install -m 0644 "$repository_root/.pi/skills/goal-plus/SKILL.md" \
    "$payload_root/pi/goal-plus/SKILL.md"
install -m 0644 "$repository_root/.pi/prompts/search-candidate-worker-thinkthread.md" \
    "$payload_root/pi/search-candidate-worker-thinkthread.md"
install -m 0644 "$repository_root/src/goal_plus/assets/thinkthread-agent-posix-bridge.mjs" \
    "$payload_root/bridge/thinkthread-agent-posix-bridge.mjs"
install -m 0755 "$repository_root/scripts/goal-plus-pi-tool-installed" \
    "$payload_root/bin/goal-plus-pi-tool"

if [[ -n "$thinkthread_source" ]]; then
    sdk_source="$thinkthread_source/sdk/agent-posix/ts"
    [[ -f "$sdk_source/package.json" && -f "$sdk_source/package-lock.json" ]] || \
        die "ThinkThread source does not contain sdk/agent-posix/ts: $thinkthread_source"
    sdk_build="$transaction_root/agent-posix-sdk"
    package_output="$transaction_root/package"
    mkdir -p -- "$sdk_build" "$package_output"
    cp -R -- "$sdk_source/." "$sdk_build/"
    (
        cd -- "$sdk_build"
        npm ci --no-audit --no-fund
        npm pack --pack-destination "$package_output" >/dev/null
    )
    sdk_package="$(find "$package_output" -maxdepth 1 -type f -name '*.tgz' -print -quit)"
fi

[[ -f "$sdk_package" ]] || die "Agent POSIX SDK package is unavailable: $sdk_package"
(
    cd -- "$payload_root"
    # npm otherwise walks to an ancestor package.json.  In a normal global
    # install that can mutate ~/.local instead of the transactional payload.
    npm init --yes >/dev/null
    npm install --omit=dev --no-audit --no-fund "$sdk_package"
)

sdk_entry="$payload_root/node_modules/@thinkthread/agent-posix/dist/index.js"
[[ -f "$sdk_entry" ]] || die "installed Agent POSIX SDK has no dist/index.js"
node --input-type=module - "$sdk_entry" <<'NODE'
import { pathToFileURL } from "node:url";

const expectedFingerprint = "fcc80b665cd990f9d1e3681a9d384cb99994f2b739cd4fbddc97bdda01391131";
const entry = process.argv[2];
const sdk = await import(pathToFileURL(entry).href);
if (sdk.CONTROL_PROTOCOL_VERSION !== 2 || sdk.CONTRACT_FINGERPRINT !== expectedFingerprint) {
    throw new Error(
        `unsupported Agent POSIX SDK: protocol=${sdk.CONTROL_PROTOCOL_VERSION}, fingerprint=${sdk.CONTRACT_FINGERPRINT}`,
    );
}
NODE

preserved_models_file="$transaction_root/preserved-models.txt"
if [[ -f "$profile_path" ]]; then
    "$payload_root/venv/bin/python" - "$profile_path" >"$preserved_models_file" <<'PY'
from pathlib import Path
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

path = Path(sys.argv[1])
with path.open("rb") as stream:
    payload = tomllib.load(stream)
for model in payload.get("delegation", {}).get("models", {}).get("allow", []):
    if isinstance(model, str):
        print(model)
PY
else
    : >"$preserved_models_file"
fi
for model in "${requested_models[@]}"; do
    printf '%s\n' "$model" >>"$preserved_models_file"
done

if [[ -d "$install_root" ]]; then
    mv -- "$install_root" "$previous_install"
fi
mv -- "$payload_root" "$install_root"
installed_new=true

if [[ -f "$profile_path" ]]; then
    mv -- "$profile_path" "$previous_profile"
fi
install -m 0600 "$repository_root/.thinkthread/pi-goal-plus.toml" "$profile_path"
profile_replaced=true

while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    "$tt_bin" model allow "$model" --profile pi-goal-plus
done < <(sort -u "$preserved_models_file")

success=true
printf 'Installed Goal Plus: %s\n' "$install_root"
printf 'Installed Profile: %s\n' "$profile_path"
printf 'Launch from a target workspace with: tt pi-goal-plus\n'
