# 远端仓库与 Waifu 插件比对采集说明

这份文档用于从云服务器上采集 `langbot` / `waifu` 相关仓库信息，回传后即可和本地 `openqqwaifu` 项目做结构与差异比对。

## 安全边界

- 不要把私钥继续发到聊天里，也不要继续复用已经出现在聊天记录中的旧私钥。
- 建议先轮换 SSH key：
  1. 生成新的密钥对
  2. 更新服务器上的 `~/.ssh/authorized_keys`
  3. 删除本地 `Downloads` 里的旧 `.key`

## 目标

采集这三类信息：

1. 服务器上有哪些 Git 仓库
2. 每个仓库的 remote / 最近提交 / 工作区状态
3. Waifu 插件目录的文件结构、Python 行数和核心文本文件列表

## 方式一：直接手工执行

先找仓库：

```bash
cd ~
find . -maxdepth 3 -name '.git' -type d 2>/dev/null
```

然后对每个仓库执行：

```bash
cd <repo-path>
git remote -v
git log --oneline -15
git status
ls -la
```

如果已经确定 Waifu 插件路径，再额外执行：

```bash
cd <path-to-waifu-plugin>
git remote -v
git log --oneline -10
find . -type f \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.md' \) \
  ! -path './.git/*' ! -path './__pycache__/*' | sort
wc -l $(find . -type f -name '*.py' ! -path './.git/*' ! -path './__pycache__/*')
```

## 方式二：一键采集

如果服务器是 Linux，推荐直接执行下面这段脚本。它会把结果汇总到一个目录里，便于打包回传。

```bash
cd ~
mkdir -p repo-audit
cat > repo-audit/collect_repo_audit.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

SCAN_ROOT="${1:-$HOME}"
WAIFU_PATH="${2:-}"
OUT_DIR="$HOME/repo-audit/output_$(date +%Y%m%d_%H%M%S)"

mkdir -p "$OUT_DIR/repos"

find "$SCAN_ROOT" -maxdepth 3 -name '.git' -type d 2>/dev/null | sort > "$OUT_DIR/git_dirs.txt"

while IFS= read -r gitdir; do
  [ -n "$gitdir" ] || continue
  repo_path="$(dirname "$gitdir")"
  repo_name="$(echo "$repo_path" | sed 's#^./##; s#^/##; s#[/ ]#_#g')"
  {
    echo "# Repo: $repo_path"
    echo
    echo "## git remote -v"
    (cd "$repo_path" && git remote -v) || true
    echo
    echo "## git log --oneline -15"
    (cd "$repo_path" && git log --oneline -15) || true
    echo
    echo "## git status"
    (cd "$repo_path" && git status) || true
    echo
    echo "## ls -la"
    (cd "$repo_path" && ls -la) || true
  } > "$OUT_DIR/repos/${repo_name}.md"
done < "$OUT_DIR/git_dirs.txt"

if [ -n "$WAIFU_PATH" ] && [ -d "$WAIFU_PATH" ]; then
  {
    echo "# Waifu Plugin: $WAIFU_PATH"
    echo
    echo "## git remote -v"
    (cd "$WAIFU_PATH" && git remote -v) || true
    echo
    echo "## git log --oneline -10"
    (cd "$WAIFU_PATH" && git log --oneline -10) || true
    echo
    echo "## file list"
    (cd "$WAIFU_PATH" && find . -type f \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.md' \) \
      ! -path './.git/*' ! -path './__pycache__/*' | sort) || true
    echo
    echo "## python line counts"
    (cd "$WAIFU_PATH" && find . -type f -name '*.py' ! -path './.git/*' ! -path './__pycache__/*' -print0 | xargs -0 wc -l) || true
  } > "$OUT_DIR/waifu-plugin.md"
fi

echo "$OUT_DIR"
SH

chmod +x repo-audit/collect_repo_audit.sh
~/repo-audit/collect_repo_audit.sh ~ <path-to-waifu-plugin>
```

执行完后，终端会输出一个结果目录，例如：

```text
/home/ubuntu/repo-audit/output_20260414_213000
```

建议再打一个压缩包：

```bash
cd /home/ubuntu/repo-audit
tar -czf repo-audit-results.tar.gz output_*
```

## 优先回传哪些文件

如果不方便整包传回，至少把下面这些内容贴回来：

- `git_dirs.txt`
- `repos/*.md`
- `waifu-plugin.md`

## 回传后我会做什么

拿到这些结果后，可以继续做这几件事：

1. 判断服务器上的 `langbot`、插件仓库、私有改动分别在哪些目录
2. 根据 `git remote -v` 确定它们分别来自哪个上游项目
3. 和本地 `openqqwaifu` 的 `src/waifu_standalone/` 做结构映射
4. 判断当前独立版和线上 Waifu 插件之间还差多少功能
5. 给出“继续迁移”还是“直接替换”的建议

## 本地对照参考

当前本地项目核心目录：

```text
src/waifu_standalone/
├── app.py
├── cells/
├── organs/
├── systems/
├── gateways/
├── templates/
└── web/
```

所以回传时，最有价值的是：

- Waifu 插件的 `*.py`
- 角色卡 / 配置文件 `*.yaml`, `*.yml`, `*.json`
- 插件自己的 `README.md` 或说明文件

## 最简交付格式

如果你只想把结果贴给另一个模型，推荐按这个顺序贴：

1. `git_dirs.txt`
2. 每个仓库的 `repos/<repo>.md`
3. `waifu-plugin.md`

这样最容易让对方快速进入比对阶段。
