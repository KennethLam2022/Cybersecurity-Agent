# ================================================
# archive-snapshot.ps1 — 创建当前代码的存档快照
# 用法: powershell scripts/archive-snapshot.ps1 [备注]
# 例:   powershell scripts/archive-snapshot.ps1 "准备实验性修改"
# ================================================

param([string]$note = "")

$DATE = Get-Date -Format "yyyyMMdd_HHmmss"
$SNAPSHOT_DIR = "archive\snapshots\$DATE"
$NOTE_FILE = "$SNAPSHOT_DIR\_说明.txt"

# 创建目录
New-Item -ItemType Directory -Path $SNAPSHOT_DIR -Force | Out-Null

# 导出当前 Git 状态
git log --oneline -5 > "$SNAPSHOT_DIR\_最近提交历史.txt"
git diff --stat > "$SNAPSHOT_DIR\_未提交修改.txt" 2>$null
git status --short > "$SNAPSHOT_DIR\_工作区状态.txt"

# 归档关键源文件（保留完整路径）
$SRC_DIR = "packages\agent\src"
$FILES = @(
    "llm_provider.py",
    "agent.py",
    "main.py",
    "api.py",
    "rag_engine.py",
    "templates\index.html",
    "templates\admin.html",
    "static\style.css"
)

foreach ($f in $FILES) {
    $srcPath = "$SRC_DIR\$f"
    if (Test-Path $srcPath) {
        $destDir = "$SNAPSHOT_DIR\$SRC_DIR\$(Split-Path $f)"
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        Copy-Item $srcPath "$SNAPSHOT_DIR\$SRC_DIR\$f" -Force
    }
}

# 生成说明
@"
存档时间: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
当前分支: $(git branch --show-current)
最近提交: $(git log --oneline -1)
备注: $note
"@ | Out-File -FilePath $NOTE_FILE -Encoding utf8

Write-Host ""
Write-Host "📦 快照已创建: archive\snapshots\$DATE" -ForegroundColor Green
Write-Host "   文件数: $($FILES.Count)" -ForegroundColor Cyan
if ($note) {
    Write-Host "   备注: $note" -ForegroundColor Cyan
}
Write-Host ""
Write-Host "恢复到当前状态的命令:" -ForegroundColor Yellow
Write-Host "   git checkout ." -ForegroundColor White