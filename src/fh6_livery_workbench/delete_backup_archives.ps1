param([Parameter(Mandatory=$true)][string]$PlanPath,[Parameter(Mandatory=$true)][string]$ReceiptPath)
$ErrorActionPreference='Stop'
function Get-LiverySha256([string]$FilePath){
  $hash=[Security.Cryptography.SHA256]::Create();$stream=[IO.File]::OpenRead($FilePath)
  try{return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
  finally{$stream.Dispose();$hash.Dispose()}
}
$plan=Get-Content -LiteralPath $PlanPath -Raw -Encoding utf8 | ConvertFrom-Json
if($plan.kind -ne 'rolling-full-save-backup-prune'){throw 'Unexpected deletion plan'}
$root=(Resolve-Path -LiteralPath $plan.project_root).Path.TrimEnd('\')
$keep=(Resolve-Path -LiteralPath $plan.retained.archive).Path
$deleted=@()
try{
  if((Get-LiverySha256 $keep) -ne $plan.retained.sha256){throw 'Retained backup changed'}
  foreach($entry in $plan.entries){
    $target=(Resolve-Path -LiteralPath $entry.path).Path
    if(-not $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase) -or $target -eq $keep){throw 'Archive path is outside exact project boundary'}
    $item=Get-Item -LiteralPath $target -Force
    if($item.PSIsContainer -or $item.Extension -ne '.zip'){throw 'Only a registered full-save ZIP file may be removed'}
    $walk=$item
    while($walk){
      if($walk.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse point refused'}
      $walk=if($walk.PSIsContainer){$walk.Parent}else{$walk.Directory}
    }
    if($item.Length -ne $entry.bytes -or (Get-LiverySha256 $target) -ne $entry.sha256){throw 'Archive changed after validation'}
    # Nonrecursive, literal, exact file deletion. Locked files are not forced.
    Remove-Item -LiteralPath $target -ErrorAction Stop
    $deleted+=@{path=$target;bytes=$entry.bytes;sha256=$entry.sha256}
  }
  @{status='PRUNED';retained=$keep;deleted=$deleted;preservedManifestsAndJournals=$true} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
}catch{
  @{status='PRUNE_DEFERRED';retained=$keep;deleted=$deleted;error=$_.Exception.Message} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
  throw
}
