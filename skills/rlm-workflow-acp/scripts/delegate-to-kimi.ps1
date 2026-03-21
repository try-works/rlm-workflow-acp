[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Run,
  [string]$Slice = "",
  [string]$Worktree = "",
  [string]$SessionName = "",
  [ValidateSet("implement", "review", "repair")][string]$Mode = "implement",
  [ValidateSet("auto", "persistent", "exec")][string]$SessionPolicy = "auto",
  [switch]$SaveTranscript,
  [int]$MaxReviewLoops = 2,
  [switch]$InitHandoff,
  [string]$DelegatedPhases = "3,4",
  [string]$FixtureSource = "",
  [string]$FixtureTest = "",
  [string]$TestCommand = "",
  [string]$RoleTemplate = "",
  [string[]]$OwnedWriteFiles = @(),
  [string[]]$AllowedReadPaths = @(),
  [ValidateSet("", "handoff_outcome", "defects_or_no_defects", "repair_summary", "patch_plan")][string]$OutputContract = "",
  [switch]$AllowSealedOverride,
  [switch]$ValidateOnly,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs
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

  $py3 = Get-Command python3 -ErrorAction SilentlyContinue
  if ($null -ne $py3) {
    return @{
      Exe = $py3.Source
      PrefixArgs = @()
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
$script = Join-Path $PSScriptRoot "delegate-to-kimi.py"

$argsList = @()
$argsList += $pythonPrefixArgs
$argsList += @($script, "--run", $Run)
if (-not [string]::IsNullOrWhiteSpace($Slice)) {
  $argsList += @("--slice", $Slice)
}
$argsList += @("--mode", $Mode)
$argsList += @("--session-policy", $SessionPolicy)
if ($SaveTranscript) {
  $argsList += @("--save-transcript")
}
$argsList += @("--max-review-loops", [string]$MaxReviewLoops)
if (-not [string]::IsNullOrWhiteSpace($Worktree)) {
  $argsList += @("--worktree", $Worktree)
}
if (-not [string]::IsNullOrWhiteSpace($SessionName)) {
  $argsList += @("--session-name", $SessionName)
}
if (-not [string]::IsNullOrWhiteSpace($RoleTemplate)) {
  $argsList += @("--role-template", $RoleTemplate)
}
foreach ($ownedWriteFile in $OwnedWriteFiles) {
  if (-not [string]::IsNullOrWhiteSpace($ownedWriteFile)) {
    $argsList += @("--owned-write-files", $ownedWriteFile)
  }
}
foreach ($allowedReadPath in $AllowedReadPaths) {
  if (-not [string]::IsNullOrWhiteSpace($allowedReadPath)) {
    $argsList += @("--allowed-read-paths", $allowedReadPath)
  }
}
if (-not [string]::IsNullOrWhiteSpace($OutputContract)) {
  $argsList += @("--output-contract", $OutputContract)
}
if ($AllowSealedOverride) {
  $argsList += @("--allow-sealed-override")
}
if ($InitHandoff) {
  $argsList += @("--init-handoff")
  if (-not [string]::IsNullOrWhiteSpace($DelegatedPhases)) {
    $argsList += @("--delegated-phases", $DelegatedPhases)
  }
  if (-not [string]::IsNullOrWhiteSpace($FixtureSource)) {
    $argsList += @("--fixture-source", $FixtureSource)
  }
  if (-not [string]::IsNullOrWhiteSpace($FixtureTest)) {
    $argsList += @("--fixture-test", $FixtureTest)
  }
  if (-not [string]::IsNullOrWhiteSpace($TestCommand)) {
    $argsList += @("--test-command", $TestCommand)
  }
}

if ($ValidateOnly) {
  $argsList += @("--validate-only")
}

if ($null -ne $ExtraArgs -and $ExtraArgs.Count -gt 0) {
  $argsList += $ExtraArgs
}

& $pythonExe @argsList
exit $LASTEXITCODE
