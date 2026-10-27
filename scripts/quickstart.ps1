# 一键上手：检查环境 -> 建 venv -> 装依赖 -> 跑 demo -> 起前后端
#
# 为什么需要这个脚本：
#   陌生人拿到仓库后，会在 3 分钟内决定要不要继续。而手动路径有四个坑：
#   ① 必须是 Python 3.12（依赖版本锁死，3.11/3.13 都可能装不上）
#   ② pip 直连 PyPI 在国内很慢甚至超时
#   ③ 要起两个进程（后端 :8000 + 前端 :5173），顺序错了前端一片空白
#   ④ 端口被占用时没人告诉他
#   这个脚本把四件事都兜住，失败时给出**下一步该干什么**，而不是抛一段红字。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1 -DemoOnly     # 只跑 demo，不起服务
#   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1 -NoOpen      # 不自动开浏览器
#   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1 -Mirror https://pypi.org/simple   # 换源

param(
  [string]$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple",
  [switch]$SkipInstall,
  [switch]$DemoOnly,
  [switch]$NoOpen
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg, $hint) {
  Write-Host "`n[失败] $msg" -ForegroundColor Red
  if ($hint) { Write-Host $hint -ForegroundColor Yellow }
  exit 1
}

# ---------- 1. 找 Python 3.12 ----------
Step "检查 Python（需要 3.12.x）"
$py = $null
foreach ($cand in @("py -3.12", "python")) {
  $parts = $cand.Split(" ")
  $exe = $parts[0]; $arg = if ($parts.Length -gt 1) { $parts[1] } else { $null }
  try {
    $ver = if ($arg) { & $exe $arg --version 2>&1 } else { & $exe --version 2>&1 }
    if ($ver -match "3\.12\.") { $py = @{ Exe = $exe; Arg = $arg }; Write-Host "    找到 $ver" -ForegroundColor Green; break }
  } catch { }
}
if (-not $py) {
  Fail "没有找到 Python 3.12" @"
装一个再回来（3.11 或 3.13 不保证能装上依赖）：
  https://www.python.org/downloads/release/python-3128/
安装时勾选 "Add python.exe to PATH"，然后重开一个终端重跑本脚本。
"@
}

# ---------- 2. 建 venv ----------
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  Step "创建虚拟环境 .venv"
  if ($py.Arg) { & $py.Exe $py.Arg -m venv .venv } else { & $py.Exe -m venv .venv }
  if (-not (Test-Path $venvPython)) { Fail "虚拟环境创建失败" "删掉 .venv 目录重试；或在 PowerShell 里手动执行 python -m venv .venv 看报错。" }
} else {
  Write-Host "    .venv 已存在，跳过创建" -ForegroundColor Green
}

# ---------- 3. 装依赖 ----------
if (-not $SkipInstall) {
  Step "安装依赖（源：$Mirror）"
  & $venvPython -m pip install -q --upgrade pip 2>&1 | Out-Null
  & $venvPython -m pip install -q -r backend\requirements.txt -i $Mirror 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Fail "依赖安装失败" @"
常见原因与处理：
  1) 网络慢/超时 -> 换源重试：-Mirror https://mirrors.aliyun.com/pypi/simple
  2) 报 "No matching distribution" -> Python 版本不是 3.12（用 py -3.12 --version 确认）
  3) 报编译错误 -> 缺 MSVC 构建工具；多数包有 wheel，通常是版本不对导致的连带问题
手动排查：.venv\Scripts\python.exe -m pip install -r backend\requirements.txt -v
"@
  }
} else {
  Write-Host "    跳过安装（-SkipInstall）" -ForegroundColor Yellow
}

# ---------- 4. 跑 demo（零凭据，验证链路真的通） ----------
Step "跑内置 demo（不需要任何 API key）"
& $venvPython backend\tests\fixtures\demo\run_demo.py 2>&1 | Select-Object -Last 6
if ($LASTEXITCODE -ne 0) { Fail "demo 没跑通" "这一步失败说明依赖或代码有问题，先别起服务。把上面的报错发给我。" }

if ($DemoOnly) {
  Write-Host "`n[demo 已通过] 链路是通的。要起页面加 -DemoOnly:$false 或直接运行不带参数。" -ForegroundColor Green
  exit 0
}

# ---------- 5. 端口检查 ----------
Step "检查端口 8000 / 5173"
foreach ($port in @(8000, 5173)) {
  $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "    端口 $port 已被占用（进程：$owner）" -ForegroundColor Yellow
    Write-Host "    -> 先关掉它，或改 README 里的端口参数" -ForegroundColor Yellow
  }
}

# ---------- 6. 起后端 ----------
Step "启动后端 http://127.0.0.1:8000"
$be = Start-Process -FilePath $venvPython `
  -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--port", "8000" `
  -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4
try {
  $h = Invoke-RestMethod "http://127.0.0.1:8000/api/healthz" -TimeoutSec 8
  Write-Host "    healthz: status=$($h.status) db=$($h.db) checkpointer=$($h.checkpointer)" -ForegroundColor Green
} catch {
  Fail "后端没起来（PID $($be.Id)）" @"
排查顺序：
  1) 端口 8000 是否被别的进程占了
  2) 直接前台跑一次看完整报错：
     .venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
"@
}

# ---------- 7. 起前端 ----------
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
  Write-Host "`n没找到 npm，跳过前端。后端 API 已经可用：http://127.0.0.1:8000/docs" -ForegroundColor Yellow
} else {
  Step "启动前端 http://localhost:5173（首次会下载依赖，可能要 1-2 分钟）"
  $fe = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "cd frontend && npm install --no-fund --no-audit && npm run dev" `
    -PassThru -WindowStyle Hidden
  $fePid = $fe.Id
  Write-Host "    前端进程 PID $fePid（首次启动请等 1-2 分钟再刷新页面）"
}

# ---------- 8. 收尾 ----------
Write-Host ""
Write-Host "==================== 可以开始了 ====================" -ForegroundColor Green
Write-Host "  页面   http://localhost:5173"
Write-Host "  API 文档 http://127.0.0.1:8000/docs"
Write-Host "  健康检查 http://127.0.0.1:8000/api/healthz"
Write-Host ""
# 只停**本脚本启动的**进程树：绝不能给"杀掉所有 python/node"这种命令——
# 那会连带终止使用者自己的项目、IDE 和未保存的工作。
# 为什么用 taskkill /T 而不是 Stop-Process -Id：
#   前端是 cmd.exe 里跑 `npm run dev`，真正占 5173 的 node 是它的**子进程**；
#   只杀 cmd.exe 会留下孤儿 node 继续占着端口，下次再跑就落到"端口被占用"分支，
#   而使用者会误以为是脚本坏了。
Write-Host "  停止服务（/T = 连子进程一起杀，不加会留下占端口的 node/python）："
Write-Host ("    taskkill /PID {0} /T /F    # 后端 uvicorn" -f $be.Id)
if ($fePid) {
  Write-Host ("    taskkill /PID {0} /T /F    # 前端 npm run dev" -f $fePid)
} else {
  Write-Host "    （本次没有启动前端）"
}
Write-Host "  确认端口真的释放（没有输出就对了）："
Write-Host "    Get-NetTCPConnection -LocalPort 8000,5173 -ErrorAction SilentlyContinue"
Write-Host "===================================================" -ForegroundColor Green

if (-not $NoOpen) {
  Write-Host "`n3 秒后打开浏览器…（不想自动打开下次加 -NoOpen）"
  Start-Sleep -Seconds 3
  try {
    Start-Process "http://localhost:5173"
  } catch {
    # 不静默吞掉：没有默认浏览器 / 被策略拦截时，至少告诉他自己点哪里。
    Write-Host "    浏览器没能自动打开，请手动访问 http://localhost:5173" -ForegroundColor Yellow
  }
}
