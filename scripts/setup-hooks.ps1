# ================================================
# setup-hooks.ps1 — 一键安装 Git hooks + 配置
# 用法: powershell scripts/setup-hooks.ps1
# ================================================

Write-Host "🔧 配置 Git hooks..." -ForegroundColor Cyan

# 设置 hooks 路径为 .githooks（受 Git 追踪）
git config core.hooksPath .githooks
Write-Host "   core.hooksPath → .githooks  ✅" -ForegroundColor Green

# 启用 Git 自动 GC（减少仓库膨胀）
git config gc.auto 1

# 显示当前配置
Write-Host ""
Write-Host "📋 当前 Git 配置:" -ForegroundColor Cyan
git config --list | Select-String "core.hooksPath|user.name|user.email"
Write-Host ""
Write-Host "✅ 完成！.githooks/pre-commit 已启用" -ForegroundColor Green
Write-Host "   核心文件被删除时会自动拦截"