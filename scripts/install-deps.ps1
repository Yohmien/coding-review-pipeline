[CmdletBinding()]
param(
    [string]$CodexSkillsDir = (Join-Path $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }) 'skills')
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Copy-LocalSkill {
    param(
        [string]$Source,
        [string]$Name
    )
    $dest = Join-Path $CodexSkillsDir $Name
    if (Test-Path -LiteralPath $dest) {
        Write-Host "skip $Name (already installed)"
        return
    }
    New-Item -ItemType Directory -Force -Path $CodexSkillsDir | Out-Null
    Copy-Item -LiteralPath $Source -Destination $dest -Recurse
    Write-Host "installed $Name"
}

function Install-FromRepo {
    param(
        [string]$Repo,
        [string]$Path,
        [string]$Name
    )
    $dest = Join-Path $CodexSkillsDir $Name
    if (Test-Path -LiteralPath $dest) {
        Write-Host "skip $Name (already installed)"
        return
    }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('codex-deps-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        git clone --depth 1 $Repo (Join-Path $tmp 'repo') | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git clone failed: $Repo" }
        New-Item -ItemType Directory -Force -Path $CodexSkillsDir | Out-Null
        if ($Path -eq '.') {
            Get-ChildItem -LiteralPath (Join-Path $tmp 'repo') -Force | Copy-Item -Destination $dest -Recurse
        }
        else {
            Copy-Item -LiteralPath (Join-Path $tmp ('repo/' + $Path)) -Destination $dest -Recurse
        }
        $gitDir = Join-Path $dest '.git'
        if (Test-Path -LiteralPath $gitDir) {
            Remove-Item -LiteralPath $gitDir -Recurse -Force
        }
        Write-Host "installed $Name"
    }
    finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Recurse -Force
        }
    }
}

Copy-LocalSkill -Source (Join-Path $RepoRoot 'skills/coding-review-pipeline') -Name 'coding-review-pipeline'
Copy-LocalSkill -Source (Join-Path $RepoRoot 'vendor/skills/search-gates') -Name 'search-gates'
Install-FromRepo -Repo 'https://github.com/mattpocock/skills.git' -Path 'skills/engineering/grill-with-docs' -Name 'grill-with-docs'
Install-FromRepo -Repo 'https://github.com/mattpocock/skills.git' -Path 'skills/productivity/grilling' -Name 'grilling'
Install-FromRepo -Repo 'https://github.com/mattpocock/skills.git' -Path 'skills/engineering/domain-modeling' -Name 'domain-modeling'
Install-FromRepo -Repo 'https://github.com/obra/superpowers.git' -Path 'skills/verification-before-completion' -Name 'verification-before-completion'
Install-FromRepo -Repo 'https://github.com/obra/superpowers.git' -Path 'skills/systematic-debugging' -Name 'systematic-debugging'
Install-FromRepo -Repo 'https://github.com/obra/superpowers.git' -Path 'skills/test-driven-development' -Name 'test-driven-development'
Install-FromRepo -Repo 'https://github.com/DietrichGebert/ponytail.git' -Path 'skills/ponytail' -Name 'ponytail'
Install-FromRepo -Repo 'https://github.com/Sxuan-Coder/alibaba-java-development-guide.git' -Path '.' -Name 'alibaba-java-development-guide'

Write-Host ''
Write-Host "Done. All skills installed to $CodexSkillsDir"

Write-Host ''
Write-Host '提示：CodeGraph 是 search-gates 的工具级前置依赖（CLI + MCP，非 skill），不随本脚本安装。'
$cg = Get-Command codegraph -ErrorAction SilentlyContinue
if ($cg) {
    Write-Host "  检测到 codegraph：$($cg.Source)"
} else {
    Write-Host '  未检测到 codegraph；请按 README 官方命令安装（irm .../install.ps1 | iex 或 npm i -g @colbymchenry/codegraph），再运行 codegraph install，并在目标项目执行 codegraph init。'
}
