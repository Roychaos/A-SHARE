# 一键推送代码到 GitHub（幂等，可重复执行）
# 用法（PowerShell）:
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -Repo "https://github.com/用户名/仓库名.git"
param([Parameter(Mandatory=$true)][string]$Repo)

git init
git config user.email "deploy@local"
git config user.name "Deploy Bot"

git add .
git commit -m "init: A股量价选股 agent"

git branch -M main
cmd /c "git remote remove origin >nul 2>&1"
git remote add origin $Repo
git push -u origin main

Write-Host ""
Write-Host "推送完成。接下来: 1) 仓库设 Public  2) Actions 配 SERVERCHAN_SENDKEY  3) 手动 Run workflow"
