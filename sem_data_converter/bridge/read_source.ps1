# SEM Data Converter database bridge, adapted from upstream read_oip.ps1 (MIT).
# Parameterized paths, bounded ownership, complete export and explicit disposal.
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$SqlCeDll
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
trap {
    [Console]::Error.WriteLine($_.Exception.ToString())
    [Console]::Error.WriteLine($_.ScriptStackTrace)
    exit 1
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$sourcePath = [IO.Path]::GetFullPath($Source)
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (-not [IO.File]::Exists($sourcePath)) { throw 'Source database does not exist.' }
if (-not [IO.File]::Exists($SqlCeDll)) { throw 'Microsoft SQL CE assembly does not exist.' }
if (-not [IO.Directory]::Exists($destinationPath)) { throw 'Export directory must already exist.' }
if (@(Get-ChildItem -LiteralPath $destinationPath -Force).Count -ne 0) { throw 'Export directory must be empty.' }
Add-Type -Path ([IO.Path]::GetFullPath($SqlCeDll))
Add-Type -AssemblyName System.Runtime.Serialization
$copy = Join-Path $destinationPath 'working.sdf'
[IO.File]::Copy($sourcePath, $copy, $false)
$builder = New-Object System.Data.SqlServerCe.SqlCeConnectionStringBuilder
$builder.set_DataSource([string]$copy)
$engine = New-Object System.Data.SqlServerCe.SqlCeEngine($builder.ConnectionString)
try { $engine.Upgrade() } finally { $engine.Dispose() }
$builder.set_FileMode('Read Only')
$builder.set_TempFilePath([string]$destinationPath)
$connection = New-Object System.Data.SqlServerCe.SqlCeConnection($builder.ConnectionString)
try {
    $connection.Open()
    $command = $connection.CreateCommand()
    try {
        $command.CommandText = 'SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION'
        $reader = $command.ExecuteReader()
        $schema = New-Object 'System.Collections.Generic.List[object]'
        try {
            while ($reader.Read()) {
                $schema.Add([ordered]@{table=$reader.GetString(0);column=$reader.GetString(1);type=$reader.GetString(2)})
            }
        } finally { $reader.Dispose() }
        $tables = New-Object 'System.Collections.Generic.List[object]'
        $tableIndex = 0
        foreach ($table in @($schema | ForEach-Object { $_.table } | Select-Object -Unique)) {
            $command.CommandText = 'SELECT * FROM [' + $table.Replace(']', ']]') + ']'
            $reader = $command.ExecuteReader()
            $rows = New-Object 'System.Collections.Generic.List[object]'
            $rowIndex = 0
            try {
                while ($reader.Read()) {
                    $row = [ordered]@{}
                    for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                        $name = $reader.GetName($i)
                        $value = $reader.GetValue($i)
                        if ($value -is [DBNull]) { $row[$name] = $null }
                        elseif ($value -is [byte[]]) {
                            $stem = 'b-{0}-{1}-{2}' -f $tableIndex,$rowIndex,$i
                            [IO.File]::WriteAllBytes((Join-Path $destinationPath ($stem + '.bin')), $value)
                            $descriptor = [ordered]@{file=($stem + '.bin');bytes=$value.Length}
                            $xmlReader = $null
                            try {
                                $xmlReader = [System.Xml.XmlDictionaryReader]::CreateBinaryReader($value, [System.Xml.XmlDictionaryReaderQuotas]::Max)
                                $document = New-Object System.Xml.XmlDocument
                                $document.XmlResolver = $null
                                $document.Load($xmlReader)
                                [IO.File]::WriteAllText((Join-Path $destinationPath ($stem + '.xml')), $document.OuterXml, $utf8)
                                $descriptor['xml'] = $stem + '.xml'
                            } catch { $descriptor['xml_error'] = $_.Exception.Message }
                            finally { if ($null -ne $xmlReader) { $xmlReader.Dispose() } }
                            $row[$name] = $descriptor
                        }
                        elseif ($value -is [Guid]) { $row[$name] = $value.ToString() }
                        elseif ($value -is [DateTime]) { $row[$name] = $value.ToString('o', [Globalization.CultureInfo]::InvariantCulture) }
                        elseif ($value -is [decimal]) { $row[$name] = $value.ToString([Globalization.CultureInfo]::InvariantCulture) }
                        else { $row[$name] = $value }
                    }
                    $rows.Add($row)
                    $rowIndex++
                }
            } finally { $reader.Dispose() }
            $file = 'table-{0}.json' -f $tableIndex
            $json = ConvertTo-Json -InputObject @($rows.ToArray()) -Depth 30 -Compress
            [IO.File]::WriteAllText((Join-Path $destinationPath $file), $json, $utf8)
            $tables.Add([ordered]@{name=$table;file=$file;row_count=$rows.Count})
            Write-Output ('Exported {0}: {1} rows' -f $table,$rows.Count)
            $tableIndex++
        }
        $metadata = [ordered]@{source_name=[IO.Path]::GetFileName($sourcePath);tables=@($tables.ToArray());schema=@($schema.ToArray())}
        [IO.File]::WriteAllText((Join-Path $destinationPath 'export.json'), (ConvertTo-Json -InputObject $metadata -Depth 30 -Compress), $utf8)
    } finally { $command.Dispose() }
} finally { $connection.Dispose() }
