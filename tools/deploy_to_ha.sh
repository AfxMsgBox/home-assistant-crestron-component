#!/usr/bin/env bash
#
# Deploy the component, optional YAML, and repository dashboards to HA.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCAL_COMPONENT_DIR="${REPO_ROOT}/custom_components/crestron"
LOCAL_DIR="${REPO_ROOT}/local"
CONVERTER="${SCRIPT_DIR}/xlsx_to_yaml.py"
DASHBOARD_DEPLOYER="${SCRIPT_DIR}/deploy_dashboards.mjs"
DASHBOARD_DIR="${REPO_ROOT}/dashboard"
DASHBOARD_ONLY=false
SKIP_DASHBOARD=false
HA_URL="${HA_URL:-}"

DEFAULT_YAML="${LOCAL_DIR}/crestron.yaml"
REMOTE_COMPONENT_PARENT="/homeassistant/custom_components"
REMOTE_COMPONENT_DIR="${REMOTE_COMPONENT_PARENT}/crestron"

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
  --dashboard-dir DIR         页面 JSON 目录（默认仓库 dashboard/，递归扫描）
  --ha-url URL                HA 网页地址（默认通过 ha core info 获取端口）

其他：
  -h, --help                  显示帮助

不带任何参数时，脚本会交互式询问服务器地址、用户名和密码。密码留空时只使用
SSH key。未通过参数指定“是否生成/上传 YAML”时，也会交互式询问。
默认部署 dashboard/ 下所有 JSON 页面。已有页面逐项询问 y、n、All yes、All no；
回车默认为 n，输入结束时跳过所有剩余已有页面。新页面仍会创建。
页面部署需要本机 Node.js 22+ 和可访问的 HA 网页地址。可用 HA_TOKEN 环境变量
提供管理员令牌；否则经 SSH 读取目标 HA 现有所有者会话换取临时令牌。

示例：
  tools/deploy_to_ha.sh

  tools/deploy_to_ha.sh --host HA_HOST --user root --key --dashboard-only

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

trap cleanup EXIT INT TERM

need_value() {
    local option=$1
    local count=$2
    (( count >= 2 )) || die "${option} 缺少参数"
}

while (( $# > 0 )); do
    case "$1" in
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

if [[ "${DASHBOARD_ONLY}" == true ]]; then
    [[ "${SKIP_DASHBOARD}" == false ]] || die "--dashboard-only 不能与 --skip-dashboard 同用"
    [[ "${GENERATE_YAML}" != yes && "${UPLOAD_YAML}" != yes ]] \
        || die "--dashboard-only 不能与生成/上传 YAML 选项同用"
    GENERATE_YAML=no
    UPLOAD_YAML=no
fi

prompt_required() {
    local prompt=$1
    local result=""
    while [[ -z "${result}" ]]; do
        read -r -p "${prompt}" result
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
        read -r -p "${prompt} ${hint} " answer
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

[[ -d "${LOCAL_COMPONENT_DIR}" ]] \
    || die "找不到本地组件目录：${LOCAL_COMPONENT_DIR}"
[[ -f "${CONVERTER}" ]] \
    || die "找不到转换器：${CONVERTER}"
command -v ssh >/dev/null 2>&1 || die "找不到 ssh 命令"
if [[ "${DASHBOARD_ONLY}" == false ]]; then
    command -v scp >/dev/null 2>&1 || die "找不到 scp 命令"
fi

# Validate every page before uploading any component or changing HA.
if [[ "${SKIP_DASHBOARD}" == false ]]; then
    [[ -d "${DASHBOARD_DIR}" ]] || die "dashboard 目录不存在：${DASHBOARD_DIR}（可用 --skip-dashboard 跳过）"
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

if [[ "${DASHBOARD_ONLY}" == true ]]; then
    deploy_dashboards
    printf '页面部署完成，无需重启 Home Assistant。\n'
    exit 0
fi

# Stage a clean component tree so runtime YAML and Python caches are never
# uploaded accidentally as part of the code deployment.
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/crestron-ha-deploy.XXXXXX")"
STAGE_COMPONENT="${STAGE_ROOT}/crestron"
mkdir -p -- "${STAGE_COMPONENT}"
cp -a "${LOCAL_COMPONENT_DIR}/." "${STAGE_COMPONENT}/"
find "${STAGE_COMPONENT}" -type f \( -name '*.pyc' -o -name 'crestron.yaml' \) \
    -delete
find "${STAGE_COMPONENT}" -depth -type d -name '__pycache__' -exec rm -rf -- {} +

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
printf '部署完成。\n'
