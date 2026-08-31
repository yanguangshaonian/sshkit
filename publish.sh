#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

usage() {
    printf '%s\n' "用法: $0 [testpypi|pypi] [--dry-run] [--skip-tests] [--keep-artifacts] [--clean-only]"
}

clean_artifacts() {
    rm -rf build dist src/*.egg-info .pytest_cache
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
    find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

target=""; dry_run=0; skip_tests=0; keep_artifacts=0; clean_only=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        testpypi|pypi) target=$1 ;;
        --dry-run) dry_run=1 ;;
        --skip-tests) skip_tests=1 ;;
        --keep-artifacts|--keep-build) keep_artifacts=1 ;;
        --clean-only) clean_only=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf '错误: 未知参数: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ! command_exists python3 && ! command_exists python; then
    printf '%s\n' "错误: 未找到 python3 或 python" >&2
    exit 1
fi
if command_exists python3; then PYTHON=python3; else PYTHON=python; fi

if [ "$clean_only" -eq 1 ]; then
    clean_artifacts
    printf '%s\n' "已清理构建临时文件"
    exit 0
fi

if [ -z "$target" ]; then
    printf '%s\n' "请选择发布目标:"
    printf '%s\n' "1) TestPyPI (https://test.pypi.org/legacy/)"
    printf '%s\n' "2) PyPI (https://upload.pypi.org/legacy/)"
    while :; do
        printf '请输入 1 或 2: '
        read -r choice
        case "$choice" in
            1) target=testpypi; break ;;
            2) target=pypi; break ;;
            *) printf '%s\n' "输入无效,请重试" ;;
        esac
    done
fi

"$PYTHON" -m build --version >/dev/null 2>&1 || {
    printf '%s\n' "错误: 未安装 build,请运行: $PYTHON -m pip install build" >&2
    exit 1
}
"$PYTHON" -m twine --version >/dev/null 2>&1 || {
    printf '%s\n' "错误: 未安装 twine,请运行: $PYTHON -m pip install twine" >&2
    exit 1
}

printf '发布目标: %s\n' "$target"
if [ "$target" = "testpypi" ]; then
    repository_url=https://test.pypi.org/legacy/
else
    repository_url=https://upload.pypi.org/legacy/
fi
clean_artifacts
if [ "$skip_tests" -eq 0 ]; then "$PYTHON" -m pytest; fi
"$PYTHON" -m build
"$PYTHON" -m twine check dist/*

if [ "$dry_run" -eq 1 ]; then
    printf '%s\n' "已完成构建和校验,未上传"
else
    printf '确认上传到 %s? 请输入 y/N: ' "$target"
    read -r confirm
    case "$confirm" in
        y|Y|yes|YES)
            printf '请输入 PyPI API key (用户名固定为 __token__): '
            read -r -s twine_password
            printf '\n';
            TWINE_USERNAME=__token__ \
            TWINE_PASSWORD="$twine_password" \
                "$PYTHON" -m twine upload --repository-url "$repository_url" dist/*
            unset twine_password
            ;;
        *) printf '%s\n' "已取消上传" ;;
    esac
 fi
if [ "$keep_artifacts" -eq 0 ]; then clean_artifacts; fi