$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$artifacts = Join-Path $root 'artifacts'
[System.IO.Directory]::CreateDirectory($artifacts) | Out-Null
$name = 'session-title-icons-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.zip'
$target = Join-Path $artifacts $name
# Explicit allowlist: never package .git, .env, logs, caches or arbitrary checkout files.
$files = [ordered]@{
    'session_title_icons/__init__.py' = '__init__.py'
    'session_title_icons/plugin.yaml' = 'plugin.yaml'
    'session_title_icons/plugin.py' = 'plugin.py'
    'session_title_icons/engine.py' = 'engine.py'
    'session_title_icons/runtime.py' = 'runtime.py'
    'session_title_icons/queue.py' = 'queue.py'
    'session_title_icons/store.py' = 'store.py'
    'session_title_icons/selector.py' = 'selector.py'
    'session_title_icons/tools.py' = 'tools.py'
    'session_title_icons/confirmation.py' = 'confirmation.py'
    'session_title_icons/unicode_emoji.py' = 'unicode_emoji.py'
    'session_title_icons/data/emoji-test.txt' = 'data/emoji-test.txt'
    'session_title_icons/data/LICENSE.txt' = 'data/LICENSE.txt'
}
$stream = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew)
try {
    $zip = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($entry in $files.GetEnumerator()) {
            $source = Join-Path $root $entry.Key
            $item = Get-Item -LiteralPath $source
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Refusing linked source: $($entry.Key)"
            }
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $source, 'session-title-icons/' + $entry.Value,
                [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally { $zip.Dispose() }
} finally { $stream.Dispose() }
[pscustomobject]@{ Path = $target; SHA256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash; Files = $files.Count }
