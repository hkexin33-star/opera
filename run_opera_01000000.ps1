# 【阶段一 · 试点】仅处理 opera_dataset\01000000（《戏考》448 部）
# 全库为 opera_dataset\ 下全部合集；试点满意后再用 run_opera_full.ps1 做阶段二。
# 说明见 README.md「数据处理两阶段策略」。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# ---------- 0. 从 .env 加载密钥（若存在）----------
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
        if ($k -and -not [Environment]::GetEnvironmentVariable($k)) {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
    Write-Host "[ENV] loaded from .env" -ForegroundColor Green
}

# ---------- 0b. 环境检查 ----------
if (-not $env:MINERU_TOKEN) {
    Write-Host "缺少 MINERU_TOKEN。任选其一：" -ForegroundColor Yellow
    Write-Host '  1) $env:MINERU_TOKEN = "你的MinerU密钥"' -ForegroundColor Yellow
    Write-Host "  2) 复制 .env.example 为 .env 并填写 MINERU_TOKEN" -ForegroundColor Yellow
    exit 1
}
if (-not $env:DEEPSEEK_API_KEY) {
    Write-Host "LLM 需要 DEEPSEEK_API_KEY（可在 .env 中配置）" -ForegroundColor Yellow
}

$Collection = "01000000"
$InputDir   = Join-Path $Root "opera_dataset\$Collection"
$OutputDir  = Join-Path $Root "opera_output"
$CombineDir = Join-Path $OutputDir "combined\$Collection"
$ManifestDir = Join-Path $OutputDir "manifests"
New-Item -ItemType Directory -Force -Path $ManifestDir | Out-Null

# ---------- 1. 查看进度（不耗 API）----------
Write-Host "`n=== 1. 处理进度 ===" -ForegroundColor Cyan
python mineru_batch_convert_structured_llm_final_v8.py `
    --status-only `
    --input-dir $InputDir `
    --output-dir $OutputDir `
    --collection-prefix $Collection

# 取消下面各步注释以执行对应阶段

# ---------- 2. 冒烟测试（先跑 2 部，确认 r6 + LLM）----------
# python mineru_batch_convert_structured_llm_final_v8.py `
#     --input-dir $InputDir `
#     --output-dir $OutputDir `
#     --llm-enabled `
#     --limit 2 `
#     --no-skip-existing

# ---------- 3. 全量批处理《戏考》448 部（耗时长，建议后台）----------
# python mineru_batch_convert_structured_llm_final_v8.py `
#     --input-dir $InputDir `
#     --output-dir $OutputDir `
#     --llm-enabled `
#     --chunk-size 50 `
#     --collection-prefix $Collection `
#     --manifest (Join-Path $ManifestDir "mineru_manifest_01000000.csv") `
#     2>&1 | Tee-Object -FilePath (Join-Path $OutputDir "logs\batch_01000000_run.log") -Append

# ---------- 4. 仅刷新全库汇总表（中断后可单独跑）----------
# python mineru_batch_convert_structured_llm_final_v8.py `
#     --combine-only `
#     --output-dir $OutputDir `
#     --collection-prefix $Collection

# ---------- 5. 质量检验 + 示例图 ----------
# python analysis_starter.py --verify --collection $Collection --data-dir $OutputDir
# python analysis_starter.py --title 空城计 --collection $Collection --data-dir $OutputDir `
#     --play-dir (Join-Path $OutputDir "$Collection\01001001_空城计")

Write-Host "`n试点汇总表目录: $CombineDir\all_docs.csv" -ForegroundColor Cyan
Write-Host "完成。满意后请使用 run_opera_full.ps1 处理整个 opera_dataset。" -ForegroundColor Green
