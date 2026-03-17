[CmdletBinding()]
param(
  [string]$RunId = "",
  [string]$WorktreeRoot = "",
  [string]$SessionName = "",
  [string]$FixturePkg = "",
  [switch]$KeepWorktree
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Python {
  # Prefer the Windows Python Launcher if available; it avoids Microsoft Store alias issues.
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($null -ne $pyLauncher) {
    return @{
      Exe = $pyLauncher.Source
      PrefixArgs = @("-3")
    }
  }

  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($null -ne $py) {
    return @{
      Exe = $py.Source
      PrefixArgs = @()
    }
  }

  throw "Python not found. Install Python or ensure `py`/`python` is on PATH."
}

$pythonInfo = Resolve-Python
$pythonExe = $pythonInfo.Exe
$pythonPrefixArgs = $pythonInfo.PrefixArgs
$script = Join-Path $PSScriptRoot "smoke-acp-functional.py"

$argsList = @()
$argsList += $pythonPrefixArgs
$argsList += @($script)
if (-not [string]::IsNullOrWhiteSpace($RunId)) {
  $argsList += @("--run-id", $RunId)
}
if (-not [string]::IsNullOrWhiteSpace($WorktreeRoot)) {
  $argsList += @("--worktree-root", $WorktreeRoot)
}
if (-not [string]::IsNullOrWhiteSpace($SessionName)) {
  $argsList += @("--session-name", $SessionName)
}
if (-not [string]::IsNullOrWhiteSpace($FixturePkg)) {
  $argsList += @("--fixture-pkg", $FixturePkg)
}
if ($KeepWorktree) {
  $argsList += @("--keep-worktree")
}

& $pythonExe @argsList
exit $LASTEXITCODE
