$ErrorActionPreference = 'Stop'
$FacadePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $FacadePython)) {
    throw 'Install the local environment using the README instructions first.'
}
& $FacadePython -m facade_dspy @args
exit $LASTEXITCODE
