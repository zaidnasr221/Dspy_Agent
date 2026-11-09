$ErrorActionPreference = 'Stop'
$FacadeArchive = Join-Path (Split-Path $PSScriptRoot -Parent) 'deliverables\Facade_DSPy_Claude_Code_Source.zip'
if (Test-Path -LiteralPath $FacadeArchive) {
    throw 'The source ZIP already exists. Choose a new destination instead of overwriting it.'
}
$FacadeArchiveDirectory = Split-Path $FacadeArchive -Parent
if (-not (Test-Path -LiteralPath $FacadeArchiveDirectory)) {
    New-Item -ItemType Directory -Path $FacadeArchiveDirectory | Out-Null
}
$FacadeSourceNames = @('facade_dspy', 'tests', 'schemas', 'examples', 'evaluation', 'README.md', 'pyproject.toml', 'run.ps1', 'test_pdf_page.py', 'package_source.ps1', 'REUSED_CODE.json', 'VALIDATION_STATUS.json')
$FacadeSourceFiles = foreach ($FacadeSourceName in $FacadeSourceNames) {
    $FacadeSourcePath = Join-Path $PSScriptRoot $FacadeSourceName
    if (Test-Path -LiteralPath $FacadeSourcePath) {
        Get-ChildItem -LiteralPath $FacadeSourcePath -File -Recurse | Where-Object {
            $_.FullName -notmatch '[\\/]__pycache__[\\/]'
        }
        if (Test-Path -LiteralPath $FacadeSourcePath -PathType Leaf) {
            Get-Item -LiteralPath $FacadeSourcePath
        }
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$FacadeZip = [System.IO.Compression.ZipFile]::Open($FacadeArchive, 'Create')
try {
    foreach ($FacadeSourceFile in ($FacadeSourceFiles | Sort-Object FullName -Unique)) {
        $FacadeRelativePath = [System.IO.Path]::GetRelativePath($PSScriptRoot, $FacadeSourceFile.FullName).Replace('\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($FacadeZip, $FacadeSourceFile.FullName, $FacadeRelativePath) | Out-Null
    }
} finally {
    $FacadeZip.Dispose()
}
Write-Output $FacadeArchive
