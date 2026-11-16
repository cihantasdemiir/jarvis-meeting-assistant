<#
.SYNOPSIS
  Windows'un "OneCore" Türkçe sesini (Microsoft Tolga) klasik SAPI5 listesine kopyalar; böylece pyttsx3 görebilir.

.DESCRIPTION
  UYARI: Bu betik geliştirme ortamında (Linux) yazılmıştır, Windows'ta ÇALIŞTIRILARAK DOĞRULANMAMIŞTIR.
  Kayıt Defteri'nde HKLM altında değişiklik yapar; yönetici yetkisi gerekir. Kurumsal bilgisayarda BT
  ekibinin onayıyla kullanın.

  Varsayılan çalışma yalnızca RAPOR verir (hiçbir şeyi değiştirmez). Değişiklik için -Apply verilmelidir.
  -Apply ile önce mevcut SAPI5 ses anahtarlarının yedeği .reg dosyasına alınır.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\tolga_sapi5.ps1            # yalnızca kontrol
  powershell -ExecutionPolicy Bypass -File scripts\tolga_sapi5.ps1 -Apply     # yönetici PowerShell'de

  Geri alma: oluşturulan yedek .reg dosyasına çift tıklayın veya
  reg delete "HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\<ANAHTAR_ADI>" /f
#>
param([switch]$Apply)

$source = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
$targets = @("HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens", "HKLM\SOFTWARE\WOW6432Node\Microsoft\Speech\Voices\Tokens")

if (-not (Test-Path $source)) {
    Write-Host "OneCore ses listesi bulunamadı ($source). Ayarlar > Saat ve dil > Konuşma bölümünden Türkçe ses paketini kurun."
    exit 1
}
$voices = Get-ChildItem $source | Where-Object { $_.PSChildName -match "trTR|tr-TR|Tolga" }
if (-not $voices) {
    Write-Host "Türkçe OneCore sesi bulunamadı. Önce Türkçe konuşma paketini kurun ve bilgisayarı yeniden başlatın."
    exit 1
}
foreach ($voice in $voices) {
    $name = $voice.PSChildName
    foreach ($target in $targets) {
        $exists = Test-Path ("Registry::" + $target + "\" + $name)
        Write-Host ("{0} -> {1}: {2}" -f $name, $target, $(if ($exists) { "zaten var" } else { "kopyalanacak" }))
    }
}
if (-not $Apply) {
    Write-Host "`nKontrol modu: hiçbir değişiklik yapılmadı. Uygulamak için yönetici PowerShell'de -Apply ile çalıştırın."
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Yönetici yetkisi gerekiyor. PowerShell'i 'Yönetici olarak çalıştır' ile açın."
    exit 1
}
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
foreach ($target in $targets) {
    $backup = Join-Path $PSScriptRoot ("sapi5_yedek_{0}_{1}.reg" -f ($target -replace "[\\:]", "_"), $stamp)
    reg export $target $backup /y | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Yedek alındı: $backup" }
}
foreach ($voice in $voices) {
    $name = $voice.PSChildName
    $from = "HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\$name"
    foreach ($target in $targets) {
        reg copy $from "$target\$name" /s /f | Out-Null
        Write-Host ("{0} -> {1}: {2}" -f $name, $target, $(if ($LASTEXITCODE -eq 0) { "kopyalandı" } else { "HATA" }))
    }
}
Write-Host "`nKontrol: python -c `"import pyttsx3; [print(v.name) for v in pyttsx3.init().getProperty('voices')]`""
