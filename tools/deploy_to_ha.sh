#!/usr/bin/env bash
#
# Download a GitHub branch and install/update the component and dashboards.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCAL_DIR="${REPO_ROOT}/local"
DASHBOARD_DIR=""
DASHBOARD_DIR_WAS_SET=false
DASHBOARD_ONLY=false
SKIP_DASHBOARD=false
HA_URL="${HA_URL:-}"
OWNER_REPO="AfxMsgBox/home-assistant-crestron-component"
BRANCH="master"
MODE="install"
MODE_WAS_SET=false
RESTART_TIMEOUT=120
SOURCE_ROOT=""
SOURCE_SHA=""

DEFAULT_YAML="${LOCAL_DIR}/crestron.yaml"
REMOTE_COMPONENT_PARENT="/homeassistant/custom_components"
REMOTE_COMPONENT_DIR="${REMOTE_COMPONENT_PARENT}/crestron"
REMOTE_SOURCE_PARENT="/homeassistant/crestron-source"

HOST=""
SSH_USER=""
SSH_PORT="22"
PASSWORD=""
PASSWORD_WAS_SET=false
USE_KEY=false
IDENTITY_FILE=""
GENERATE_YAML=""
UPLOAD_YAML=""
XLSX_PATH=""
YAML_PATH="${DEFAULT_YAML}"
ORIGINAL_ARGC=$#
STAGE_ROOT=""

usage() {
    cat <<'EOF'
用法：
  tools/deploy_to_ha.sh [选项]

安装/更新选项：
  --install                   安装（默认），设备 YAML 保留交互选择
  --update                    更新已有集成，默认不生成/上传设备 YAML
  --branch BRANCH             GitHub 分支名（默认 master）
  --restart-timeout SECONDS   重启后等待 HA HTTP API 的秒数（默认 120）

连接选项：
  --host HOST                 HA 服务器地址或主机名
  --user USER                 SSH 用户名（交互模式默认 root）
  --port PORT                 SSH 端口（默认 22）
  --password PASSWORD         使用密码登录（会通过 sshpass 传给 ssh/scp）
  --key                       使用 SSH key 登录
  --identity FILE             指定私钥文件（同时启用 key 登录）

YAML 选项：
  --generate-yaml             重新运行 xlsx_to_yaml.py
  --skip-generate-yaml        不重新生成 YAML
  --xlsx FILE                 输入 xlsx 路径（默认 local/ 中第一个 .xlsx）
  --yaml FILE                 生成/上传的 YAML 路径（默认 local/crestron.yaml）
  --upload-yaml               上传 YAML 到测试服务器
  --skip-upload-yaml          不上传 YAML

Dashboard 选项：
  --dashboard-only            仅部署页面，不上传组件/YAML、不检查或重启 Core
  --skip-dashboard            跳过页面部署（仅部署组件/YAML）
  --dashboard-dir DIR         覆盖下载源码中的 dashboard/，使用指定本地目录
  --ha-url URL                HA 网页地址（默认通过 ha core info 获取端口）

其他：
  -h, --help                  显示帮助

不带任何参数时，脚本会交互式询问服务器地址、用户名和密码。密码留空时只使用
SSH key。安装时未指定“是否生成/上传 YAML”也会询问；更新默认保留设备 YAML。
安装、更新、仅页面模式都会从 GitHub 获取指定分支最新提交并下载完整源码；
组件和工具不使用本地工作区版本。需要 curl、python3；无需 git。
完整安装/更新会把源码保存在 HA 的 /homeassistant/crestron-source/<提交 SHA>/，组件另行部署。
默认部署下载源码中 dashboard/ 下所有 JSON 页面。已有页面逐项询问 y、n、All yes、All no；
回车默认为 n，输入结束时跳过所有剩余已有页面。新页面仍会创建。
页面部署需要本机 Node.js 22+ 和可访问的 HA 网页地址。可用 HA_TOKEN 环境变量
提供管理员令牌；否则经 SSH 读取目标 HA 现有所有者会话换取临时令牌。

示例：
  tools/deploy_to_ha.sh

  tools/deploy_to_ha.sh --update --branch dev --host HA_HOST --user root --key

  tools/deploy_to_ha.sh --branch dev --host HA_HOST --user root --key --dashboard-only

  tools/deploy_to_ha.sh \
    --host HA_HOST \
    --user root \
    --key \
    --generate-yaml \
    --upload-yaml

  HA_SSH_PASSWORD='secret' tools/deploy_to_ha.sh \
    --host HA_HOST \
    --user root \
    --upload-yaml

安全提示：
  自动化时优先使用 SSH key 或 HA_SSH_PASSWORD 环境变量。直接写
  --password 可能把密码留在 shell 历史中。
EOF
}

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${STAGE_ROOT}" && -d "${STAGE_ROOT}" ]]; then
        rm -rf -- "${STAGE_ROOT}"
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

need_value() {
    local option=$1
    local count=$2
    (( count >= 2 )) || die "${option} 缺少参数"
}

while (( $# > 0 )); do
    case "$1" in
        --install|--update)
            SELECTED_MODE=${1#--}
            if [[ "${MODE_WAS_SET}" == true && "${MODE}" != "${SELECTED_MODE}" ]]; then
                die "--install 与 --update 不能同用"
            fi
            MODE=${SELECTED_MODE}
            MODE_WAS_SET=true
            shift
            ;;
        --branch)
            need_value "$1" "$#"
            BRANCH=$2
            shift 2
            ;;
        --restart-timeout)
            need_value "$1" "$#"
            RESTART_TIMEOUT=$2
            shift 2
            ;;
        --host)
            need_value "$1" "$#"
            HOST=$2
            shift 2
            ;;
        --user)
            need_value "$1" "$#"
            SSH_USER=$2
            shift 2
            ;;
        --port)
            need_value "$1" "$#"
            SSH_PORT=$2
            shift 2
            ;;
        --password)
            need_value "$1" "$#"
            PASSWORD=$2
            PASSWORD_WAS_SET=true
            USE_KEY=false
            shift 2
            ;;
        --key)
            PASSWORD=""
            PASSWORD_WAS_SET=true
            USE_KEY=true
            shift
            ;;
        --identity)
            need_value "$1" "$#"
            IDENTITY_FILE=$2
            PASSWORD=""
            PASSWORD_WAS_SET=true
            USE_KEY=true
            shift 2
            ;;
        --generate-yaml)
            GENERATE_YAML=yes
            shift
            ;;
        --skip-generate-yaml)
            GENERATE_YAML=no
            shift
            ;;
        --xlsx)
            need_value "$1" "$#"
            XLSX_PATH=$2
            GENERATE_YAML=yes
            shift 2
            ;;
        --yaml)
            need_value "$1" "$#"
            YAML_PATH=$2
            shift 2
            ;;
        --upload-yaml)
            UPLOAD_YAML=yes
            shift
            ;;
        --skip-upload-yaml)
            UPLOAD_YAML=no
            shift
            ;;
        --dashboard-only)
            DASHBOARD_ONLY=true
            shift
            ;;
        --skip-dashboard)
            SKIP_DASHBOARD=true
            shift
            ;;
        --dashboard-dir)
            need_value "$1" "$#"
            DASHBOARD_DIR=$2
            DASHBOARD_DIR_WAS_SET=true
            shift 2
            ;;
        --ha-url)
            need_value "$1" "$#"
            HA_URL=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1（使用 --help 查看帮助）"
            ;;
    esac
done

[[ "${RESTART_TIMEOUT}" =~ ^[1-9][0-9]{0,3}$ ]] \
    || die "--restart-timeout 必须为 1..9999 的整数"

if [[ "${DASHBOARD_ONLY}" == true ]]; then
    [[ "${SKIP_DASHBOARD}" == false ]] || die "--dashboard-only 不能与 --skip-dashboard 同用"
    [[ "${GENERATE_YAML}" != yes && "${UPLOAD_YAML}" != yes ]] \
        || die "--dashboard-only 不能与生成/上传 YAML 选项同用"
    GENERATE_YAML=no
    UPLOAD_YAML=no
fi
if [[ "${MODE}" == update ]]; then
    GENERATE_YAML=${GENERATE_YAML:-no}
    UPLOAD_YAML=${UPLOAD_YAML:-no}
fi

prompt_required() {
    local prompt=$1
    local result=""
    while [[ -z "${result}" ]]; do
        read -r -p "${prompt}" result || die "输入结束，请用参数提供服务器地址"
    done
    printf '%s' "${result}"
}

ask_yes_no() {
    local prompt=$1
    local default=${2:-no}
    local answer=""
    local hint="[y/N]"
    [[ "${default}" == "yes" ]] && hint="[Y/n]"

    while true; do
        if ! read -r -p "${prompt} ${hint} " answer; then
            printf '%s' "${default}"
            return
        fi
        answer=${answer:-${default}}
        case "${answer,,}" in
            y|yes|是)
                printf 'yes'
                return
                ;;
            n|no|否)
                printf 'no'
                return
                ;;
            *)
                printf '请输入 y 或 n。\n' >&2
                ;;
        esac
    done
}

fetch_source() {
    local encoded_branch
    encoded_branch=$(python3 - "${BRANCH}" <<'PY'
import sys
from urllib.parse import quote
branch = sys.argv[1]
parts = branch.split('/')
invalid = (not branch or branch.startswith('-') or branch == '@'
           or '..' in branch or '@{' in branch
           or any(ord(c) < 33 or ord(c) == 127 or c in '~^:?*[\\' for c in branch)
           or any(not p or p.startswith('.') or p.endswith(('.', '.lock')) for p in parts))
if invalid:
    sys.exit('错误：无效 Git 分支名')
print(quote(branch, safe=''))
PY
    ) || die "无法解析分支名"
    STAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/crestron-ha-deploy.XXXXXX")
    printf '正在获取 GitHub 分支：%s（%s）\n' "${BRANCH}" "${MODE}"
    curl -fLsS --connect-timeout 10 --max-time 120 --retry 2 \
        "https://api.github.com/repos/${OWNER_REPO}/git/ref/heads/${encoded_branch}" \
        -o "${STAGE_ROOT}/ref.json" || die "获取分支失败，请检查网络及 --branch（默认 master）；尚未上传文件"
    SOURCE_SHA=$(python3 - "${STAGE_ROOT}/ref.json" "${BRANCH}" <<'PY'
import json, re, sys
with open(sys.argv[1]) as f:
    ref = json.load(f)
sha = ref.get('object', {}).get('sha', '')
if ref.get('ref') != 'refs/heads/' + sys.argv[2] or not re.fullmatch('[0-9a-f]{40}', sha):
    sys.exit('错误：GitHub 未返回所选分支的有效提交')
print(sha)
PY
    ) || die "无法确认 GitHub 提交"
    printf '正在下载提交：%s\n' "${SOURCE_SHA}"
    curl -fLsS --connect-timeout 10 --max-time 120 --retry 2 \
        "https://codeload.github.com/${OWNER_REPO}/tar.gz/${SOURCE_SHA}" \
        -o "${STAGE_ROOT}/source.tar.gz" || die "源码下载失败；尚未上传文件"
    # Only regular files/directories under one archive root may be extracted.
    SOURCE_ROOT=$(python3 - "${STAGE_ROOT}/source.tar.gz" "${STAGE_ROOT}/source" <<'PY'
from pathlib import Path, PurePosixPath
import shutil, sys, tarfile
dest = Path(sys.argv[2])
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    members = archive.getmembers()
    roots = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or not (member.isfile() or member.isdir())):
            sys.exit('错误：压缩包包含不支持的路径或链接')
        roots.add(path.parts[0])
    if len(roots) != 1:
        sys.exit('错误：压缩包必须只有一个源码根目录')
    dest.mkdir()
    for member in members:
        target = dest / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open('wb') as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    root = dest / roots.pop()
    for required in ('custom_components/crestron/manifest.json', 'tools/xlsx_to_yaml.py'):
        if not (root / required).is_file():
            sys.exit('错误：源码缺少 ' + required)
    print(root)
PY
    ) || die "源码压缩包校验或解压失败；尚未上传文件"
    LOCAL_COMPONENT_DIR="${SOURCE_ROOT}/custom_components/crestron"
    CONVERTER="${SOURCE_ROOT}/tools/xlsx_to_yaml.py"
    DASHBOARD_DEPLOYER="${SOURCE_ROOT}/tools/deploy_dashboards.mjs"
    if [[ "${DASHBOARD_DIR_WAS_SET}" == false ]]; then
        DASHBOARD_DIR="${SOURCE_ROOT}/dashboard"
        if [[ ! -d "${DASHBOARD_DIR}" && "${SKIP_DASHBOARD}" == false ]]; then
            [[ "${DASHBOARD_ONLY}" == false ]] || die "分支 ${BRANCH} 不包含 dashboard，无法仅部署页面"
            printf '分支 %s 不包含 dashboard，跳过页面部署。\n' "${BRANCH}"
            SKIP_DASHBOARD=true
        fi
    fi
}

find_default_xlsx() {
    local candidates=()
    shopt -s nullglob
    candidates=("${LOCAL_DIR}"/*.xlsx "${LOCAL_DIR}"/*.XLSX)
    shopt -u nullglob
    if (( ${#candidates[@]} > 0 )); then
        printf '%s' "${candidates[0]}"
    fi
}

if [[ -z "${HOST}" ]]; then
    HOST="$(prompt_required 'HA 服务器地址：')"
fi

if [[ -z "${SSH_USER}" ]]; then
    read -r -p "SSH 用户名 [root]：" SSH_USER
    SSH_USER=${SSH_USER:-root}
fi

# The no-argument flow explicitly asks for a password. In parameter mode,
# omitting --password means key authentication, which keeps automation
# non-interactive.
if (( ORIGINAL_ARGC == 0 )); then
    read -r -s -p "SSH 密码（留空使用 key 登录）：" PASSWORD
    printf '\n'
    PASSWORD_WAS_SET=true
    if [[ -z "${PASSWORD}" ]]; then
        USE_KEY=true
    fi
elif [[ "${PASSWORD_WAS_SET}" == false ]]; then
    if [[ -n "${HA_SSH_PASSWORD:-}" ]]; then
        PASSWORD=${HA_SSH_PASSWORD}
        PASSWORD_WAS_SET=true
    else
        USE_KEY=true
    fi
fi

# An explicitly empty --password has the same meaning as leaving the
# interactive password prompt empty: key-only authentication.
if [[ -z "${PASSWORD}" ]]; then
    USE_KEY=true
fi

[[ "${HOST}" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "服务器地址包含不支持的字符：${HOST}"
[[ "${SSH_USER}" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "用户名包含不支持的字符：${SSH_USER}"
[[ "${SSH_PORT}" =~ ^[0-9]+$ ]] \
    || die "SSH 端口必须是数字"
(( SSH_PORT >= 1 && SSH_PORT <= 65535 )) \
    || die "SSH 端口必须在 1..65535"

if [[ -n "${IDENTITY_FILE}" ]]; then
    [[ -f "${IDENTITY_FILE}" ]] || die "私钥文件不存在：${IDENTITY_FILE}"
fi

command -v ssh >/dev/null 2>&1 || die "找不到 ssh 命令"
command -v curl >/dev/null 2>&1 || die "从 GitHub 下载需要 curl"
command -v python3 >/dev/null 2>&1 || die "校验和解压源码需要 python3"
if [[ "${DASHBOARD_ONLY}" == false ]]; then
    command -v scp >/dev/null 2>&1 || die "找不到 scp 命令"
fi

fetch_source

# Validate every page before uploading any component or changing HA.
if [[ "${SKIP_DASHBOARD}" == false ]]; then
    [[ -d "${DASHBOARD_DIR}" ]] || die "dashboard 目录不存在：${DASHBOARD_DIR}（可用 --skip-dashboard 跳过）"
    [[ -f "${DASHBOARD_DEPLOYER}" ]] || die "所选分支缺少 tools/deploy_dashboards.mjs，无法部署页面"
    command -v node >/dev/null 2>&1 || die "页面部署需要 Node.js 22+（可用 --skip-dashboard 跳过）"
    node -e 'if (Number(process.versions.node.split(".")[0]) < 22 || typeof WebSocket !== "function") process.exit(1)' \
        || die "页面部署需要 Node.js 22 或更新版本"
    node "${DASHBOARD_DEPLOYER}" --dir "${DASHBOARD_DIR}" --check
fi

if [[ -n "${PASSWORD}" ]] && ! command -v sshpass >/dev/null 2>&1; then
    die "密码登录需要 sshpass；请安装 sshpass，或把密码留空改用 SSH key"
fi

if [[ -z "${GENERATE_YAML}" ]]; then
    GENERATE_YAML="$(ask_yes_no '是否重新用转换器生成 YAML？' no)"
fi

if [[ "${GENERATE_YAML}" == "yes" ]]; then
    if [[ -z "${XLSX_PATH}" ]]; then
        DEFAULT_XLSX="$(find_default_xlsx)"
        if [[ -n "${DEFAULT_XLSX}" ]]; then
            read -r -p "xlsx 文件路径 [${DEFAULT_XLSX}]：" XLSX_PATH
            XLSX_PATH=${XLSX_PATH:-${DEFAULT_XLSX}}
        else
            read -r -p "xlsx 文件路径（local 目录中未找到 .xlsx）：" XLSX_PATH
            [[ -n "${XLSX_PATH}" ]] || die "必须提供 xlsx 文件路径"
        fi
    fi
    [[ -f "${XLSX_PATH}" ]] || die "xlsx 文件不存在：${XLSX_PATH}"
    command -v python3 >/dev/null 2>&1 || die "找不到 python3"
    mkdir -p -- "$(dirname -- "${YAML_PATH}")"
    printf '正在生成 YAML：%s\n' "${YAML_PATH}"
    python3 "${CONVERTER}" "${XLSX_PATH}" "${YAML_PATH}"
fi

if [[ -z "${UPLOAD_YAML}" ]]; then
    UPLOAD_YAML="$(ask_yes_no "是否上传 ${YAML_PATH} 到测试服务器？" no)"
fi

if [[ "${UPLOAD_YAML}" == "yes" ]]; then
    [[ -f "${YAML_PATH}" ]] || die "要上传的 YAML 不存在：${YAML_PATH}"
fi

SSH_OPTIONS=(
    -p "${SSH_PORT}"
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
)
SCP_OPTIONS=(
    -O
    -P "${SSH_PORT}"
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
)

if [[ -n "${IDENTITY_FILE}" ]]; then
    SSH_OPTIONS+=(-i "${IDENTITY_FILE}")
    SCP_OPTIONS+=(-i "${IDENTITY_FILE}")
fi

if [[ "${USE_KEY}" == true ]]; then
    SSH_OPTIONS+=(
        -o BatchMode=yes
        -o PasswordAuthentication=no
        -o KbdInteractiveAuthentication=no
    )
    SCP_OPTIONS+=(
        -o BatchMode=yes
        -o PasswordAuthentication=no
        -o KbdInteractiveAuthentication=no
    )
else
    SSH_OPTIONS+=(
        -o PubkeyAuthentication=no
        -o PreferredAuthentications=password,keyboard-interactive
    )
    SCP_OPTIONS+=(
        -o PubkeyAuthentication=no
        -o PreferredAuthentications=password,keyboard-interactive
    )
fi

run_authenticated() {
    if [[ -n "${PASSWORD}" ]]; then
        SSHPASS="${PASSWORD}" sshpass -e "$@"
    else
        "$@"
    fi
}

REMOTE="${SSH_USER}@${HOST}"

deploy_dashboards() {
    if [[ "${SKIP_DASHBOARD}" == true ]]; then
        printf '已跳过 dashboard 部署。\n'
        return
    fi
    local connection=(ssh "${SSH_OPTIONS[@]}" "${REMOTE}")
    local options=(--dir "${DASHBOARD_DIR}" --backup-dir "${LOCAL_DIR}/dashboard-backups" --host "${HOST}")
    [[ -z "${HA_URL}" ]] || options+=(--ha-url "${HA_URL}")
    if [[ -n "${PASSWORD}" ]]; then
        connection=(sshpass -e "${connection[@]}")
    fi
    SSHPASS="${PASSWORD}" node "${DASHBOARD_DEPLOYER}" "${options[@]}" -- "${connection[@]}"
}

printf '正在测试 SSH 连接：%s（端口 %s）\n' "${REMOTE}" "${SSH_PORT}"
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" "test -d /homeassistant"

if [[ "${MODE}" == update && "${DASHBOARD_ONLY}" == false ]]; then
    run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" \
        "test -f '${REMOTE_COMPONENT_DIR}/manifest.json'" \
        || die "目标尚未安装 Crestron，请使用 --install"
fi

if [[ "${DASHBOARD_ONLY}" == true ]]; then
    deploy_dashboards
    printf '页面部署完成，无需重启 Home Assistant。\n'
    exit 0
fi

# Resolve the recovery endpoint before changing any remote files.
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" "ha core info --raw-json" \
    > "${STAGE_ROOT}/core-info.json"
CORE_URL=$(python3 - "${STAGE_ROOT}/core-info.json" "${HOST}" "${HA_URL}" <<'PY'
import json, sys
from urllib.parse import urlsplit
if sys.argv[3]:
    url = sys.argv[3].rstrip('/')
else:
    with open(sys.argv[1]) as f:
        info = json.load(f)
    core = info.get('data', info)
    port = core.get('port')
    if not isinstance(port, int) or not 1 <= port <= 65535:
        sys.exit('错误：无法识别 HA HTTP 端口，请使用 --ha-url')
    scheme = 'https' if core.get('ssl') else 'http'
    url = f'{scheme}://{sys.argv[2]}:{port}'
parts = urlsplit(url)
if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
    sys.exit('错误：HA 地址必须是无凭据和查询参数的 HTTP(S) 地址')
print(url + '/api/')
PY
) || die "无法确定 HA 恢复检查地址；尚未上传文件"

# Stage a clean component tree so runtime YAML and Python caches are never
# uploaded accidentally as part of the code deployment.
STAGE_COMPONENT="${STAGE_ROOT}/crestron"
mkdir -p -- "${STAGE_COMPONENT}"
cp -a "${LOCAL_COMPONENT_DIR}/." "${STAGE_COMPONENT}/"
find "${STAGE_COMPONENT}" -type f \( -name '*.pyc' -o -name 'crestron.yaml' \) \
    -delete
find "${STAGE_COMPONENT}" -depth -type d -name '__pycache__' -exec rm -rf -- {} +

# Keep the complete downloaded repository away from HA's live configuration.
# In particular its sample configuration.yaml must never replace the user's.
REMOTE_SOURCE_DIR="${REMOTE_SOURCE_PARENT}/${SOURCE_SHA}"
printf '正在保存完整源码（含工具、文档和页面）：%s\n' "${REMOTE_SOURCE_DIR}"
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" \
    "mkdir -p -- '${REMOTE_SOURCE_DIR}'"
run_authenticated scp "${SCP_OPTIONS[@]}" -p -r \
    "${SOURCE_ROOT}/." "${REMOTE}:${REMOTE_SOURCE_DIR}/"

printf '正在备份远端组件...\n'
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" \
    'set -eu; mkdir -p /homeassistant/crestron-backups; backup_dir=$(mktemp -d /homeassistant/crestron-backups/deploy.XXXXXX); if test -d /homeassistant/custom_components/crestron; then cp -a /homeassistant/custom_components/crestron "$backup_dir/crestron"; fi; echo "组件备份：$backup_dir"'

printf '正在上传组件：%s -> %s:%s\n' \
    "${LOCAL_COMPONENT_DIR}" "${REMOTE}" "${REMOTE_COMPONENT_DIR}"
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" \
    "mkdir -p -- '${REMOTE_COMPONENT_PARENT}'"
run_authenticated scp "${SCP_OPTIONS[@]}" -p -r \
    "${STAGE_COMPONENT}" "${REMOTE}:${REMOTE_COMPONENT_PARENT}/"

if [[ "${UPLOAD_YAML}" == "yes" ]]; then
    printf '正在上传 YAML：%s -> %s:%s/crestron.yaml\n' \
        "${YAML_PATH}" "${REMOTE}" "${REMOTE_COMPONENT_DIR}"
    run_authenticated scp "${SCP_OPTIONS[@]}" -p \
        "${YAML_PATH}" "${REMOTE}:${REMOTE_COMPONENT_DIR}/crestron.yaml"
else
    printf '已按用户选择跳过 YAML 上传。\n'
fi

# Validate before restarting: a restart on a broken config leaves Home
# Assistant down, and the failure is far easier to read here than in the boot
# log. This is also the point where a half-finished scp shows up.
printf '正在检查 Home Assistant 配置（ha core check）...\n'
if ! run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" "ha core check"; then
    die "配置检查未通过，已跳过重启。远端文件已更新，请修正后重新运行本脚本，或手动执行 'ha core restart'。"
fi

deploy_dashboards

printf '正在重启 Home Assistant Core...\n'
run_authenticated ssh "${SSH_OPTIONS[@]}" "${REMOTE}" "ha core restart"
printf '正在等待 Home Assistant HTTP API 恢复...\n'
READY_DEADLINE=$((SECONDS + RESTART_TIMEOUT))
while true; do
    STATUS=$(curl -s --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' "${CORE_URL}") || STATUS=000
    # /api/ is protected; 401 is the expected response without a HA token.
    if [[ "${STATUS}" == 200 || "${STATUS}" == 401 ]]; then
        break
    fi
    (( SECONDS < READY_DEADLINE )) || die "Core 已收到重启命令，但 HTTP API 未在 ${RESTART_TIMEOUT} 秒内恢复，请检查 ha core logs"
    sleep 2
done
printf '部署完成：%s / %s，HA HTTP API 已恢复。\n' "${BRANCH}" "${SOURCE_SHA}"
