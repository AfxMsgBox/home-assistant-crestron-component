#!/usr/bin/env bash
#
# 单文件启动器：准备 GitHub/本地源码，依次调用设备和自动化部署工具。

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCAL_PROJECT=false
if [[ -f "${REPO_ROOT}/custom_components/crestron/manifest.json" ]]; then
    LOCAL_PROJECT=true
    LOCAL_DIR="${REPO_ROOT}/local"
else
    LOCAL_DIR="${SCRIPT_DIR}/local"
fi
DASHBOARD_DIR=""
DASHBOARD_DIR_WAS_SET=false
DASHBOARD_ONLY=false
SKIP_DASHBOARD=false
HA_URL="${HA_URL:-}"
OWNER_REPO="AfxMsgBox/home-assistant-crestron-component"
BRANCH="master"
BRANCH_WAS_SET=false
SOURCE_KIND="local"
SOURCE_WAS_SET=false
SOURCE_ID=""
SOURCE_LABEL=""
MODE="install"
MODE_WAS_SET=false
RESTART_TIMEOUT=120
SOURCE_ROOT=""
SOURCE_SHA=""

DEFAULT_YAML="${LOCAL_DIR}/crestron.yaml"

HOST=""
SSH_USER=""
SSH_PORT="22"
PASSWORD=""
PASSWORD_WAS_SET=false
IDENTITY_FILE=""
GENERATE_YAML=""
UPLOAD_YAML=""
GENERATE_AUTOMATIONS=""
UPLOAD_AUTOMATIONS=ask
AUTOMATIONS_ONLY=false
DEVICES_ONLY=false
AUTOMATION_YAML=""
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
  --update                    更新已有集成；询问是否重新生成设备，默认不上传设备 YAML
  --source local|github       源码来源（默认 local；无本地项目时自动使用 GitHub）
  --branch BRANCH             GitHub 分支名（默认 master；不能与 local 同用）
  --restart-timeout SECONDS   重启后等待 HA HTTP API 的秒数（默认 120）

连接选项：
  --host HOST                 HA 服务器地址或主机名
  --user USER                 SSH 用户名（交互模式默认 root）
  --port PORT                 SSH 端口（默认 22）
  --password PASSWORD         使用密码登录（会通过 sshpass 传给 ssh/scp）
  --key                       使用 SSH 密钥登录
  --identity FILE             指定私钥文件（同时启用密钥登录）

YAML 选项：
  --generate-yaml             重新运行 xlsx_to_yaml.py
  --skip-generate-yaml        不重新生成 YAML
  --xlsx FILE                 输入 xlsx 路径（默认 local/ 中第一个 .xlsx）
  --yaml FILE                 生成/上传的 YAML 路径（默认 local/crestron.yaml）
  --upload-yaml               上传 YAML 到目标 HA 服务器
  --skip-upload-yaml          不上传 YAML

自动化关联选项：
  --generate-automations      设备生效后，读取 Excel 和 HA 注册表重新生成自动化
  --skip-generate-automations 跳过自动化关联生成/部署
  --devices-only              仅执行第一步，不询问/部署自动化
  --automations-only          仅执行第二步，不上传组件或设备配置，不重启 HA
  --automation-yaml FILE      自动化输出路径（默认与设备 YAML 同目录）
  --upload-automations        确认上传并覆盖服务器自动化；默认生成后询问
  --skip-upload-automations   仅生成本地自动化，不上传

仪表盘选项：
  --dashboard-only            仅部署页面，不上传组件/YAML、不检查配置或重启 HA 服务
  --skip-dashboard            跳过页面部署（仅部署组件/YAML）
  --dashboard-dir DIR         覆盖所选源码中的 dashboard/，使用指定本地目录
  --ha-url URL                HA 网页地址（默认通过 ha core info 获取端口）

其他：
  -h, --help                  显示帮助

不带参数时先选择来源：1. 本地目录（默认），2. GitHub。
本地目录由脚本所在 tools/ 的上一级推断；单独下载此 Shell 即可从 GitHub 部署。
没有本地项目时自动选择 GitHub；完整源码下载并检查后才运行两个 Python 部署步骤。
选择 GitHub 后分页读取全部分支，按编号选择（有 master 时默认 master，否则第一项）。
再询问服务器地址、用户名和密码，密码留空使用 SSH 密钥。
未指定选项时分别询问是否重新生成设备文件、自动化关联；回车或输入结束默认否。
第二步在设备重启生效后读取注册表，输出可读关联清单，确认上传后备份、检查并重载自动化。
GitHub 模式下载所选分支最新提交；local 模式复制当前工作区，包含未提交和新增源码，
排除 .git、local/、虚拟环境、缓存、.env 文件和 crestron.yaml；无需 git。
仅上传运行所需组件和选定的设备 YAML；源码、工具、文档和测试不在服务器存档。
组件与仪表盘备份保存在服务器 /homeassistant/crestron-backups/日期/时间-随机编号/。
重启后通过 SSH 在服务器端用 curl 检查 HTTP API，服务器需安装 curl。
组件、转换器、自动化模板及工具、页面工具均来自所选源码。默认部署其中 dashboard/ 下所有 JSON 页面。
已有页面可选 y（覆盖）、n（跳过）、All yes（全部覆盖）、All no（全部跳过）；
回车默认为 n，输入结束时跳过所有剩余已有页面。新页面仍会创建。
页面部署需要本机 Node.js 22+ 和可访问的 HA 网页地址。可用 HA_TOKEN 环境变量
提供管理员令牌；否则经 SSH 读取目标 HA 现有所有者会话换取临时令牌。

示例：
  tools/deploy_to_ha.sh

  tools/deploy_to_ha.sh --update --source local --host HA_HOST --user root --key

  tools/deploy_to_ha.sh --update --source github --branch dev --host HA_HOST --user root --key

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
  自动化时优先使用 SSH 密钥或 HA_SSH_PASSWORD 环境变量。直接写
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
        --source)
            need_value "$1" "$#"
            SOURCE_KIND=$2
            SOURCE_WAS_SET=true
            shift 2
            ;;
        --branch)
            need_value "$1" "$#"
            BRANCH=$2
            BRANCH_WAS_SET=true
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
            shift 2
            ;;
        --key)
            PASSWORD=""
            PASSWORD_WAS_SET=true
            shift
            ;;
        --identity)
            need_value "$1" "$#"
            IDENTITY_FILE=$2
            PASSWORD=""
            PASSWORD_WAS_SET=true
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
        --generate-automations) GENERATE_AUTOMATIONS=yes; shift ;;
        --skip-generate-automations) GENERATE_AUTOMATIONS=no; shift ;;
        --automations-only) AUTOMATIONS_ONLY=true; shift ;;
        --devices-only) DEVICES_ONLY=true; shift ;;
        --upload-automations) UPLOAD_AUTOMATIONS=yes; GENERATE_AUTOMATIONS=yes; shift ;;
        --skip-upload-automations) UPLOAD_AUTOMATIONS=no; shift ;;
        --automation-yaml)
            need_value "$1" "$#"
            AUTOMATION_YAML=$2
            shift 2
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

if [[ "${BRANCH_WAS_SET}" == true && "${SOURCE_WAS_SET}" == false ]]; then
    SOURCE_KIND=github
fi

[[ "${SOURCE_KIND}" == github || "${SOURCE_KIND}" == local ]] \
    || die "--source 只能为 github 或 local"
if [[ "${SOURCE_KIND}" == local && "${BRANCH_WAS_SET}" == true ]]; then
    die "--source local 不能与 --branch 同用"
fi

[[ "${RESTART_TIMEOUT}" =~ ^[1-9][0-9]{0,3}$ ]] \
    || die "--restart-timeout 必须为 1..9999 的整数"

if [[ "${DEVICES_ONLY}" == true ]]; then
    [[ "${AUTOMATIONS_ONLY}" == false && "${GENERATE_AUTOMATIONS}" != yes ]] \
        || die "--devices-only 不能与自动化部署选项同用"
    GENERATE_AUTOMATIONS=no
fi
if [[ "${AUTOMATIONS_ONLY}" == true ]]; then
    [[ "${DASHBOARD_ONLY}" == false && "${GENERATE_AUTOMATIONS}" != no ]] \
        || die "--automations-only 不能与 --dashboard-only 或 --skip-generate-automations 同用"
    GENERATE_YAML=no
    UPLOAD_YAML=no
    GENERATE_AUTOMATIONS=yes
    SKIP_DASHBOARD=true
fi
if [[ "${DASHBOARD_ONLY}" == true ]]; then
    [[ "${GENERATE_AUTOMATIONS}" != yes ]] || die "--dashboard-only 不能与 --generate-automations 同用"
    GENERATE_AUTOMATIONS=no
    [[ "${SKIP_DASHBOARD}" == false ]] || die "--dashboard-only 不能与 --skip-dashboard 同用"
    [[ "${GENERATE_YAML}" != yes && "${UPLOAD_YAML}" != yes ]] \
        || die "--dashboard-only 不能与生成/上传 YAML 选项同用"
    GENERATE_YAML=no
    UPLOAD_YAML=no
fi
if [[ "${MODE}" == update ]]; then
    UPLOAD_YAML=${UPLOAD_YAML:-no}
fi
if [[ "${GENERATE_AUTOMATIONS}" == no && "${UPLOAD_AUTOMATIONS}" == yes ]]; then
    die "上传自动化需要重新生成关联，不能与跳过生成同用"
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
                printf '请输入 y（是）或 n（否）。\n' >&2
                ;;
        esac
    done
}

fetch_source() {
    local encoded_branch mode_label="安装"
    [[ "${MODE}" != update ]] || mode_label="更新"
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
    printf '正在获取 GitHub 分支：%s（%s）\n' "${BRANCH}" "${mode_label}"
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
    # 仅解压单一根目录下的普通文件和目录，拒绝越界路径及链接。
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
    SOURCE_ID=${SOURCE_SHA}
    SOURCE_LABEL="GitHub ${BRANCH}"
}

prepare_source_paths() {
    DEVICE_DEPLOYER="${SOURCE_ROOT}/tools/deploy_devices.py"
    AUTOMATION_GENERATOR="${SOURCE_ROOT}/tools/xlsx_to_automations.py"
    AUTOMATION_DEPLOYER="${SOURCE_ROOT}/tools/deploy_automations.py"
    AUTOMATION_TEMPLATE="${SOURCE_ROOT}/automations_manual_template.yaml"
    [[ -f "${SOURCE_ROOT}/tools/deploy_common.py" ]] \
        || die "所选源码缺少 tools/deploy_common.py；请先将新版部署工具发布到所选分支"
    if [[ "${AUTOMATIONS_ONLY}" == false ]]; then
        [[ -f "${DEVICE_DEPLOYER}" ]] \
            || die "所选源码缺少 tools/deploy_devices.py；请先将新版部署工具发布到所选分支"
    fi
}

select_github_branch() {
    local page=1 answer index default=1
    local branches=() batch=()
    command -v curl >/dev/null 2>&1 || die "读取 GitHub 分支需要 curl"
    printf '正在读取 GitHub 全部分支：%s\n' "${OWNER_REPO}"
    while true; do
        curl -fLsS --connect-timeout 10 --max-time 120 --retry 2 \
            "https://api.github.com/repos/${OWNER_REPO}/branches?per_page=100&page=${page}" \
            -o "${STAGE_ROOT}/branches.json" \
            || die "读取 GitHub 分支失败，请检查网络或 API 限额；尚未连接 HA"
        python3 - "${STAGE_ROOT}/branches.json" > "${STAGE_ROOT}/branch-names" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
if not isinstance(data, list) or any(
    not isinstance(b, dict) or not isinstance(b.get('name'), str)
    or not b['name'] or any(ord(c) < 33 or ord(c) == 127 for c in b['name'])
    for b in data
):
    sys.exit('错误：GitHub 分支列表格式不正确')
for branch in data:
    print(branch['name'])
PY
        mapfile -t batch < "${STAGE_ROOT}/branch-names"
        branches+=("${batch[@]}")
        (( ${#batch[@]} == 100 )) || break
        page=$((page + 1))
    done
    (( ${#branches[@]} > 0 )) || die "GitHub 仓库没有可选分支"
    for index in "${!branches[@]}"; do
        printf '  %d. %s\n' "$((index + 1))" "${branches[index]}"
        [[ "${branches[index]}" != master ]] || default=$((index + 1))
    done
    while true; do
        read -r -p "请选择分支编号 [${default}：${branches[default-1]}]：" answer \
            || die "输入结束，未选择 GitHub 分支"
        answer=${answer:-${default}}
        if [[ "${answer}" =~ ^[0-9]{1,6}$ ]]; then
            index=$((10#${answer}))
            if (( index >= 1 && index <= ${#branches[@]} )); then
                BRANCH=${branches[index-1]}
                return
            fi
        fi
        printf '请输入列表中的分支编号。\n' >&2
    done
}

select_source() {
    local answer
    if [[ "${LOCAL_PROJECT}" == false ]]; then
        printf '未检测到本地项目，使用 GitHub 来源。\n'
        SOURCE_KIND=github
        select_github_branch
        return
    fi
    printf '请选择部署来源：\n  1. 本地目录：%s（默认）\n  2. GitHub：%s\n' "${REPO_ROOT}" "${OWNER_REPO}"
    while true; do
        read -r -p '来源编号 [1]：' answer || die "输入结束，未选择部署来源"
        case "${answer:-1}" in
            1) SOURCE_KIND=local; return ;;
            2) SOURCE_KIND=github; select_github_branch; return ;;
            *) printf '请输入 1 或 2。\n' >&2 ;;
        esac
    done
}

snapshot_local_source() {
    printf '正在准备本地源码：%s\n' "${REPO_ROOT}"
    SOURCE_ROOT="${STAGE_ROOT}/source/local"
    SOURCE_ID=$(python3 - "${REPO_ROOT}" "${SOURCE_ROOT}" "${STAGE_ROOT}" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import shutil, stat, sys, uuid

root, dest, stage = (Path(p).resolve() for p in sys.argv[1:])
excluded = {'.git', 'local', '__pycache__', '.mypy_cache', '.pytest_cache',
            '.ruff_cache', '.venv', 'venv', 'node_modules', '.env', 'crestron.yaml'}

def copy_tree(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if source == root and item.name in {'test', 'tests'} and item.is_dir():
            continue
        if (item.name in excluded or item.name.startswith('.env.')
                or item.suffix in {'.pyc', '.pyo'} or item == stage):
            continue
        mode = item.lstat().st_mode
        if stat.S_ISDIR(mode):
            copy_tree(item, target / item.name)
        elif stat.S_ISREG(mode):
            shutil.copy2(item, target / item.name)
        else:
            sys.exit(f'错误：本地源码包含链接或特殊文件：{item}')

for required in ('custom_components/crestron/manifest.json', 'tools/xlsx_to_yaml.py'):
    if not (root / required).is_file():
        sys.exit(f'错误：本地项目缺少 {required}，请将脚本放在项目 tools/ 目录')
copy_tree(root, dest)
print('local-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12])
PY
    ) || die "准备本地源码失败；尚未连接 HA"
    SOURCE_LABEL="本地 ${REPO_ROOT}"
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

command -v python3 >/dev/null 2>&1 || die "准备源码需要 python3"
STAGE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/crestron-ha-deploy.XXXXXX")
# Python 独立入口仅增加步骤参数，也保留来源/连接的交互体验。
INTERACTIVE=false
if (( ORIGINAL_ARGC == 0 )) || { (( ORIGINAL_ARGC == 1 )) && [[ "${DEVICES_ONLY}" == true || "${AUTOMATIONS_ONLY}" == true ]]; }; then
    INTERACTIVE=true
    select_source
elif [[ "${LOCAL_PROJECT}" == false && "${SOURCE_WAS_SET}" == false && "${BRANCH_WAS_SET}" == false ]]; then
    printf '未检测到本地项目，使用 GitHub master。\n'
    SOURCE_KIND=github
fi

if [[ -z "${HOST}" ]]; then
    HOST="$(prompt_required 'HA 服务器地址：')"
fi

if [[ -z "${SSH_USER}" ]]; then
    read -r -p "SSH 用户名 [root]：" SSH_USER
    SSH_USER=${SSH_USER:-root}
fi

# 无参数时询问密码；带参数时按显式密码、环境变量、密钥的顺序选择登录方式。
if [[ "${INTERACTIVE}" == true ]]; then
    read -r -s -p "SSH 密码（留空使用密钥登录）：" PASSWORD
    printf '\n'
    PASSWORD_WAS_SET=true
elif [[ "${PASSWORD_WAS_SET}" == false ]]; then
    if [[ -n "${HA_SSH_PASSWORD:-}" ]]; then
        PASSWORD=${HA_SSH_PASSWORD}
        PASSWORD_WAS_SET=true
    fi
fi
# Python 公共连接模块把空密码视为密钥登录。

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
command -v curl >/dev/null 2>&1 || die "GitHub 下载或 HA 恢复检查需要 curl"
command -v python3 >/dev/null 2>&1 || die "校验和解压源码需要 python3"
if [[ "${DASHBOARD_ONLY}" == false && "${AUTOMATIONS_ONLY}" == false ]]; then
    command -v scp >/dev/null 2>&1 || die "找不到 scp 命令"
fi

if [[ "${SOURCE_KIND}" == github ]]; then
    fetch_source
else
    snapshot_local_source
fi
prepare_source_paths

if [[ -n "${PASSWORD}" ]] && ! command -v sshpass >/dev/null 2>&1; then
    die "密码登录需要 sshpass；请安装 sshpass，或把密码留空改用 SSH 密钥"
fi

if [[ -z "${GENERATE_YAML}" ]]; then
    GENERATE_YAML="$(ask_yes_no '是否从 Excel 重新生成设备文件 crestron.yaml（会覆盖本地输出文件）？' no)"
fi

if [[ -z "${GENERATE_AUTOMATIONS}" ]]; then
    GENERATE_AUTOMATIONS="$(ask_yes_no '是否重新生成自动化关联（设备生效后读取 Excel 和 HA 注册表）？' no)"
fi
if [[ -z "${AUTOMATION_YAML}" ]]; then
    AUTOMATION_YAML="$(dirname -- "${YAML_PATH}")/automations_manual.yaml"
fi

if [[ "${GENERATE_YAML}" == yes || "${GENERATE_AUTOMATIONS}" == yes ]]; then
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
fi
if [[ "${GENERATE_AUTOMATIONS}" == yes ]]; then
    for required in "${AUTOMATION_GENERATOR}" "${AUTOMATION_DEPLOYER}" "${AUTOMATION_TEMPLATE}"; do
        [[ -f "${required}" ]] || die "所选源码缺少自动化工具/模板：${required}"
    done
    python3 - "${YAML_PATH}" "${AUTOMATION_YAML}" <<'PY_CHECK'
from pathlib import Path
import sys
if Path(sys.argv[1]).resolve() == Path(sys.argv[2]).resolve():
    sys.exit('错误：设备文件与自动化输出不能使用同一路径')
PY_CHECK
    python3 "${AUTOMATION_GENERATOR}" "${XLSX_PATH}" --template "${AUTOMATION_TEMPLATE}" --check-xlsx
fi
if [[ "${UPLOAD_YAML}" == "yes" && "${GENERATE_YAML}" != yes ]]; then
    [[ -f "${YAML_PATH}" ]] || die "要上传的 YAML 不存在：${YAML_PATH}"
fi

# 密码仅经子进程环境传递，不进入命令行或阶段结果文件。
export HA_SSH_PASSWORD="${PASSWORD}"
CONNECTION=(--host "${HOST}" --user "${SSH_USER}" --port "${SSH_PORT}")
[[ -z "${IDENTITY_FILE}" ]] || CONNECTION+=(--identity "${IDENTITY_FILE}")
[[ -z "${HA_URL}" ]] || CONNECTION+=(--ha-url "${HA_URL}")
RESULT_FILE="${STAGE_ROOT}/device-result.json"

if [[ "${AUTOMATIONS_ONLY}" == false ]]; then
    DEVICE_OPTIONS=(--prepared "${CONNECTION[@]}" --source-root "${SOURCE_ROOT}"
        --yaml "${YAML_PATH}" --restart-timeout "${RESTART_TIMEOUT}" --result-file "${RESULT_FILE}")
    [[ "${MODE}" != update ]] || DEVICE_OPTIONS+=(--update)
    [[ -z "${XLSX_PATH}" ]] || DEVICE_OPTIONS+=(--xlsx "${XLSX_PATH}")
    [[ "${GENERATE_YAML}" != yes ]] || DEVICE_OPTIONS+=(--generate-yaml)
    [[ "${UPLOAD_YAML}" != yes ]] || DEVICE_OPTIONS+=(--upload-yaml)
    [[ "${UPLOAD_YAML}" != no ]] || DEVICE_OPTIONS+=(--skip-upload-yaml)
    [[ "${SKIP_DASHBOARD}" != true ]] || DEVICE_OPTIONS+=(--skip-dashboard)
    [[ "${DASHBOARD_ONLY}" != true ]] || DEVICE_OPTIONS+=(--dashboard-only)
    [[ "${DASHBOARD_DIR_WAS_SET}" != true ]] || DEVICE_OPTIONS+=(--dashboard-dir "${DASHBOARD_DIR}")
    [[ "${GENERATE_AUTOMATIONS}" != yes ]] || DEVICE_OPTIONS+=(--require-automations)
    python3 -B "${DEVICE_DEPLOYER}" "${DEVICE_OPTIONS[@]}"
fi
if [[ "${GENERATE_AUTOMATIONS}" == yes ]]; then
    printf '\n第二步：生成并部署自动化关联\n'
    AUTOMATION_OPTIONS=(--prepared "${CONNECTION[@]}" --xlsx "${XLSX_PATH}"
        --output "${AUTOMATION_YAML}" --template "${AUTOMATION_TEMPLATE}"
        --wait-seconds "${RESTART_TIMEOUT}" --upload "${UPLOAD_AUTOMATIONS}")
    [[ ! -f "${RESULT_FILE}" ]] || AUTOMATION_OPTIONS+=(--context "${RESULT_FILE}")
    python3 -B "${AUTOMATION_DEPLOYER}" "${AUTOMATION_OPTIONS[@]}"
else
    printf '已跳过第二步：自动化关联生成。\n'
fi
printf '部署完成：%s / %s。\n' "${SOURCE_LABEL}" "${SOURCE_ID}"
